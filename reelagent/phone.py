"""iPhone mode: the phone is the server (a-Shell + Shortcuts, no PC, no cloud, no Claude usage).

  Share a clip → the Shortcut saves it into reelagent-data/inbox and runs  `python3 -m reelagent phone`
    1. picks up every new video (inbox, files given on the command line, videos dropped in Documents)
    2. works out the game (AI looks at the footage), finds the best moments, grades and edits the reels,
       writes the hook, caption and hashtags from this week's trends and Instagram's own top posts
    3. posts the best one straight away when it's a sensible hour, queues the rest
    4. writes one line to reelagent-data/phone_status.txt for the Shortcut's notification

  Automations run  `python3 -m reelagent phone-tick`  (posting times, Instagram opened, charger plugged in):
    posts a queued reel when one is due (catching up on slots the phone slept through), answers comments,
    refreshes trends and the Instagram scout, learns from the stats.

An iPhone can't wake itself up to post at an exact minute, so posting is "catch-up": every time a tick
runs it posts if a slot has passed today that hasn't had its reel yet, within your active hours and
with a minimum gap between posts.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import autopilot, media, pipeline, reach
from .config import HOME, ROOT, is_ios
from .db import DB, copy_of

STATUS_FILE = "phone_status.txt"


def _pc(cfg) -> dict:
    return cfg.get("phone") or {}


def _tz(cfg) -> ZoneInfo:
    return ZoneInfo(cfg.get("timezone", "America/Toronto"))


def _t(s) -> dtime:
    h, m = map(int, str(s).split(":"))
    return dtime(h, m)


def _now(cfg, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(_tz(cfg))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def in_active_hours(cfg, now: datetime | None = None) -> bool:
    lo, hi = (_pc(cfg).get("active_hours") or ["10:00", "23:30"])[:2]
    t = _now(cfg, now).time()
    return _t(lo) <= t <= _t(hi)


def _posted_today(cfg, db: DB, now: datetime | None = None) -> list:
    n = _now(cfg, now)
    return db.posted_since(_iso(n.replace(hour=0, minute=0, second=0, microsecond=0)))


def gap_ok(cfg, db: DB, now: datetime | None = None) -> bool:
    last = db.last_posted()
    if not last or not last["posted_at"]:
        return True
    gap = timedelta(hours=float(_pc(cfg).get("min_gap_hours", 2.5)))
    return _now(cfg, now) - datetime.fromisoformat(last["posted_at"]).astimezone(_tz(cfg)) >= gap


def under_daily_max(cfg, db: DB, now: datetime | None = None) -> bool:
    return len(_posted_today(cfg, db, now)) < int(cfg["schedule"]["max_per_day"])


def due(cfg, db: DB, now: datetime | None = None) -> bool:
    """Catch-up rule: a posting slot has passed today that hasn't had its reel yet."""
    n = _now(cfg, now)
    if not (in_active_hours(cfg, n) and under_daily_max(cfg, db, n) and gap_ok(cfg, db, n)):
        return False
    passed = [s for s in reach.post_times(cfg) if _t(s) <= n.time()]
    return len(passed) > len(_posted_today(cfg, db, n))


def post_after_edit_ok(cfg, db: DB, now: datetime | None = None) -> bool:
    rule = str(_pc(cfg).get("post_after_edit", "smart"))
    if rule == "queue":
        return due(cfg, db, now)
    if rule == "now":
        return under_daily_max(cfg, db, now) and gap_ok(cfg, db, now)
    return in_active_hours(cfg, now) and under_daily_max(cfg, db, now) and gap_ok(cfg, db, now)


def next_slots(cfg, db: DB, n: int = 3) -> list[str]:
    now = _now(cfg)
    later = [s for s in reach.post_times(cfg) if _t(s) > now.time()]
    tomorrow = [f"tomorrow {s}" for s in reach.post_times(cfg)]
    return (later + tomorrow)[:n]


# ── getting clips in ────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[^\w.-]+", "_", stem).strip("._") or "clip"
    return f"{stem[:60]}{ext.lower()}"


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, ext = dest.stem, dest.suffix
    for i in range(2, 999):
        cand = dest.with_name(f"{stem}_{i}{ext}")
        if not cand.exists():
            return cand
    return dest.with_name(f"{stem}_{int(time.time())}{ext}")


def gather(cfg, files: list[str] | None = None) -> list[Path]:
    """Bring shared/dropped videos into the inbox under shell-safe names. Returns what's there now."""
    inbox = cfg.inbox
    moved = []
    for f in files or []:
        p = Path(f).expanduser()
        if p.is_file() and p.suffix.lower() in pipeline.VIDEO_EXT:
            if inbox in p.resolve().parents:
                continue                              # already in the inbox
            dest = _unique(inbox / _safe_name(p.name))
            try:
                p.rename(dest)
            except OSError:
                shutil.copy2(p, dest)                 # other volume / read-only share: copy instead
            moved.append(dest)
    if _pc(cfg).get("sweep_documents", True):
        docs = [Path.home() / "Documents"] if is_ios() else []
        for d in docs:
            for p in d.glob("*"):
                if p.is_file() and p.suffix.lower() in pipeline.VIDEO_EXT and not p.name.startswith("."):
                    dest = _unique(inbox / _safe_name(p.name))
                    try:
                        p.rename(dest)
                        moved.append(dest)
                    except OSError:
                        pass
    for p in list(inbox.rglob("*")):        # names from Photos/Shortcuts can contain spaces
        if p.is_file() and p.suffix.lower() in pipeline.VIDEO_EXT and _safe_name(p.name) != p.name:
            try:
                p.rename(_unique(p.with_name(_safe_name(p.name))))
            except OSError:
                pass
    return moved


def _write_status(cfg, text: str) -> None:
    try:
        (HOME / STATUS_FILE).write_text(text.strip() + "\n", encoding="utf-8")
    except OSError:
        pass


# ── commands ────────────────────────────────────────────────────────────────

def run(cfg, files: list[str] | None = None, game: str | None = None, dry_run: bool = False) -> str:
    """Shared clip(s) → finished reels → post one if it's a good moment → status line."""
    db = DB(cfg.state_dir / "agent.db")
    gather(cfg, files)
    recs = pipeline.new_recordings(cfg, db, settle_seconds=0)   # the Shortcut saves the file before running us
    made: list[str] = []
    errors = []
    if not recs:
        pipeline.log("No new clips in the inbox. Share a gameplay video to the Reel Agent shortcut.", cfg)
    for rec in recs:
        try:
            made += pipeline.process_file(cfg, db, rec, game)
        except Exception as e:
            errors.append(f"{rec.name}: {e}")
            pipeline.log(f"❌ could not edit {rec.name}: {e}", cfg)
            db.mark_source(rec.resolve(), "error", str(e)[:300])

    posted = None
    connected = bool(cfg.ig_user_id and cfg.ig_token)
    if made and not autopilot.paused(cfg) and (connected or dry_run) and post_after_edit_ok(cfg, db):
        row = autopilot.pick_next(cfg, db)
        if row and autopilot.post(cfg, db, row, dry_run=dry_run):
            posted = row

    parts = []
    if made:
        hooks = [copy_of(db.get(i)).get("hook_text", "") for i in made]
        parts.append(f"🎬 {len(made)} reel{'s' if len(made) != 1 else ''} made: " + " · ".join(f'"{h}"' for h in hooks[:3]))
    elif recs and not errors:
        parts.append("Clip checked: no moment was strong enough to post (or it was pre-release / a cutscene).")
    if posted:
        parts.append(f"✅ Posted \"{copy_of(posted).get('hook_text', '')}\"" + (" (dry run)" if dry_run else ""))
    waiting = len(db.clips("queued"))
    if waiting:
        when = ", ".join(next_slots(cfg, db, 2))
        parts.append(f"⏳ {waiting} in the queue, next posting time {when}")
    if made and not connected and not dry_run:
        parts.append("Instagram isn't connected yet: run  python3 -m reelagent phone-setup")
    if errors:
        parts.append("⚠️ " + errors[0][:160])
    status = "\n".join(parts) or "Nothing to do."
    _write_status(cfg, status)
    print("\n" + status)
    db.conn.close()
    return status


def tick(cfg, dry_run: bool = False) -> str:
    """For automations: post if a slot is due (catch-up), then the light account jobs."""
    db = DB(cfg.state_dir / "agent.db")
    before = db.last_posted()
    try:
        autopilot.tick(cfg, db, dry_run=dry_run, due=due, process_inbox=False)
    except Exception as e:
        pipeline.log(f"! tick: {e}", cfg)
    after = db.last_posted()
    waiting = len(db.clips("queued"))
    if after and (not before or after["id"] != before["id"]):
        status = f"✅ Posted \"{copy_of(after).get('hook_text', '')}\". {waiting} left in the queue."
    elif not waiting:
        status = "📭 Queue empty: share more gameplay to the Reel Agent shortcut."
    else:
        status = f"⏳ {waiting} queued, next posting time {', '.join(next_slots(cfg, db, 1))}."
    if not (cfg.ig_user_id and cfg.ig_token) and not dry_run:
        status += " (Instagram not connected yet)"
    _write_status(cfg, status)
    print(status)
    db.conn.close()
    return status


# ── setup + update ──────────────────────────────────────────────────────────

def _pip(*pkgs: str) -> None:
    if is_ios():
        os.system("pip install --quiet " + " ".join(pkgs))   # a-Shell's pip (pure-Python packages)
    else:
        os.system(f'"{sys.executable}" -m pip install --quiet ' + " ".join(pkgs))


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        v = ""
    return v or default


def speed_test(cfg) -> float:
    """Edit a 6-second synthetic clip with the full look; returns seconds taken."""
    from . import editor, looks
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "test.mp4"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6",
                   "-f", "lavfi", "-i", "sine=f=440:d=6", "-shortest", *media.encode_args(23, "ultrafast"),
                   "-pix_fmt", "yuv420p", "-c:a", "aac", src.name], cwd=Path(td))
        info = media.probe(src)
        look = looks.plan(cfg, "gta6", {"mean": 120, "p2": 10, "p98": 240})
        spec = editor.RenderSpec(source=src, info=info, start=0.0, end=6.0, peak=3.0, hook="SPEED TEST",
                                 handle="@test", teaser=False, look=look, encoder=cfg["clips"].get("encoder", "auto"),
                                 font=cfg["clips"].get("font", "Anton"))
        t0 = time.time()
        editor.render(spec, Path(td) / "out.mp4")
        return time.time() - t0


def setup(cfg) -> None:
    from . import instagram
    from .config import update_env
    print("Reel Agent · iPhone setup\n")
    print("1/6  Python packages")
    _pip("requests", "python-dotenv", "tzdata")
    print("2/6  FFmpeg")
    if not media.tool_ok("ffmpeg") and is_ios():
        os.system("pkg install ffmpeg")
        media.tool_ok.cache_clear()
    if not media.tool_ok("ffmpeg"):
        print("  ❌ ffmpeg isn't available. In a-Shell run:  pkg install ffmpeg   then run setup again.")
        return
    enc = media.video_encoder(cfg["clips"].get("encoder", "auto"))
    print(f"  ✅ ffmpeg {media.ffmpeg_major()} · video encoder: {enc}"
          + ("" if media.has_filter("ass") else "  ⚠️ no caption burn-in (libass) in this build"))

    print("3/6  Your page")
    user = cfg.user_config()
    handle = _ask("  Instagram handle for the corner watermark (e.g. @frameperfect), blank = none",
                  user.get("handle", ""))
    tz = _ask("  Your time zone", user.get("timezone", cfg.get("timezone", "America/Toronto")))
    user.update({"handle": handle, "timezone": tz})
    cfg.save_user_config(user)

    print("4/6  Free AI brain + trend data")
    if not os.getenv("GEMINI_API_KEY"):
        k = _ask("  Gemini API key (free at aistudio.google.com/apikey), blank = skip")
        if k:
            update_env({"GEMINI_API_KEY": k})
    if not os.getenv("YOUTUBE_API_KEY"):
        k = _ask("  YouTube Data API key (free, optional, for trend data), blank = skip")
        if k:
            update_env({"YOUTUBE_API_KEY": k})

    print("5/6  Instagram")
    if cfg.ig_user_id and cfg.ig_token:
        print("  ✅ already connected")
    elif _ask("  Connect Instagram now? (y/n)", "y").lower().startswith("y"):
        try:
            instagram.setup_interactive(cfg)
        except Exception as e:
            print(f"  ❌ {e}\n  You can retry any time:  python3 -m reelagent setup-instagram")

    print("6/6  Speed test (edits a 6-second test clip with the full pro look)")
    try:
        secs = speed_test(cfg)
        per_reel = secs / 6 * 26 * 1.15
        print(f"  ✅ took {secs:.0f}s → a 24-second reel takes about {max(per_reel / 60, 0.2):.1f} min on this phone")
    except Exception as e:
        print(f"  ❌ test edit failed: {str(e)[-600:]}")
        return
    print(f"\nAll set. Your data lives in {HOME}\nShare a gameplay clip to the Reel Agent shortcut to make your first reel.")


def update(cfg, repo: str | None = None) -> None:
    """Download the latest code from GitHub over the code folder. Your data folder is never touched."""
    import io
    import zipfile

    import requests
    repo = repo or (cfg.get("update") or {}).get("repo", "mxmedits/reel-agent")
    url = f"https://github.com/{repo}/archive/refs/heads/main.zip"
    print(f"Downloading {url} ...")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    keep = {"config.yaml", ".env", "inbox", "output", "music", "voices"}
    n = 0
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts[1:]        # drop the "reel-agent-main/" prefix
            if not parts or parts[0] in keep or info.is_dir():
                continue
            dest = ROOT.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(info))
            n += 1
    print(f"✅ Updated {n} files in {ROOT}. Your settings, keys and reels were left alone.")
