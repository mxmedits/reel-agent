"""The hands-off loop: new recordings → reels → posted on schedule → comments answered → stats learned."""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import captions, engagement, instagram, pipeline, reach, scout, trends
from .db import DB, copy_of, now_iso

SLOT_WINDOW = timedelta(hours=2)   # a slot missed by more than this (server down) is skipped, not crammed in
WAKE = threading.Event()           # the dashboard sets this to run a cycle right away
JOB_LOCK = threading.Lock()        # one heavy job (render / upload) at a time
STATE = {"last_tick": None, "busy": ""}


def paused(cfg) -> bool:
    return bool((cfg.get("autopilot") or {}).get("paused", False))


def _tz(cfg):
    return ZoneInfo(cfg.get("timezone", "America/Toronto"))


def _local_midnight_utc(cfg) -> str:
    now = datetime.now(_tz(cfg))
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="seconds")


def slot_due(cfg, db: DB) -> bool:
    tz, now = _tz(cfg), datetime.now(_tz(cfg))
    sch = cfg["schedule"]
    if len(db.posted_since(_local_midnight_utc(cfg))) >= int(sch["max_per_day"]):
        return False
    for hhmm in reach.post_times(cfg):
        h, m = map(int, str(hhmm).split(":"))
        slot = datetime.combine(now.date(), dtime(h, m), tzinfo=tz)
        if slot <= now < slot + SLOT_WINDOW:
            if not db.posted_since(slot.astimezone(timezone.utc).isoformat(timespec="seconds")):
                return True
    return False


def pick_next(cfg, db: DB):
    rows = [r for r in db.clips("queued") if pipeline.released(cfg.game_profile(r["game"]))]
    if not rows:
        return None
    weights = {}
    lp = cfg.state_dir / "learnings.json"
    if lp.exists():
        try:
            weights = json.loads(lp.read_text(encoding="utf-8")).get("game_weights", {}) or {}
        except ValueError:
            pass
    last = db.last_posted()

    def rank(r):
        variety = 1 if (last and r["game"] == last["game"]) else 0          # alternate games
        return (variety, -(r["quality"] or 0) * (1 + 0.15 * (weights.get(r["game"], 1) or 1)), r["created_at"])

    return sorted(rows, key=rank)[0]


def post(cfg, db: DB, row, dry_run: bool = False, log=pipeline.log) -> bool:
    copy = copy_of(row)
    text = captions.instagram_text(copy)
    file = Path(row["file"])
    if not file.exists():
        db.update(row["id"], status="failed", error="video file missing")
        return False
    if dry_run:
        log(f"[dry-run] would post {row['id']} ({file.name}):\n{text}", cfg)
        return True
    render = json.loads(row["render_json"] or "{}")
    igc = cfg.get("instagram") or {}
    try:
        ig = instagram.from_config(cfg)
        # reach boosters: a trending Instagram sound that fits, and trial reels for less certain clips
        sound = None
        if igc.get("trending_audio", True) and not flagged_audio(cfg):
            recent = [copy_of(r).get("ig_audio", {}).get("audio_id") for r in db.posted_since("1970")[-6:]]
            try:
                sound = reach.pick_sound(cfg, ig, copy, row["game"], [x for x in recent if x],
                                         log=lambda m: log(m, cfg))
            except Exception as e:
                log(f"  (no Instagram sound: {e})", cfg)
        audio = None
        if sound:
            voiced = bool(render.get("voice"))
            audio = {"audio_id": sound["audio_id"],
                     "audio_volume": 25 if voiced else int(igc.get("audio_volume", 70)),
                     "video_volume": 100 if voiced else int(igc.get("video_volume", 55))}
        trial = reach.trial_strategy(cfg, row)

        ladder = [(audio, trial)]               # if Instagram refuses an option, fall back without it
        if trial:
            ladder.append((audio, None))
        if audio:
            ladder.append((None, None))
        res, used, last = None, (None, None), None
        for a, t in ladder:
            try:
                res = ig.publish_reel(file, text, share_to_feed=igc.get("share_to_feed", True),
                                      thumb_offset_ms=render.get("cover_ms"), audio=a, trial=t)
                used = (a, t)
                break
            except instagram.IGRateLimited:
                raise
            except instagram.IGError as e:
                last = e
                if t:
                    reach.set_flag(cfg, "trial_unavailable", 7)
                    log(f"  trial reels not available on this account yet ({e}); posting normally", cfg)
                elif a:
                    reach.set_flag(cfg, "audio_unavailable", 1)
                    log(f"  Instagram sound refused ({e}); posting with the reel's own audio", cfg)
        if res is None:
            raise last or instagram.IGError("publish failed")
        if used[0] and sound:
            copy["ig_audio"] = sound
        copy["trial"] = bool(used[1])
        db.update(row["id"], status="posted", ig_status="posted", ig_id=res["id"], ig_url=res.get("permalink"),
                  posted_at=now_iso(), error=None, copy_json=copy)
        extra = []
        if used[0] and sound:
            extra.append(f"sound: {sound['title']} - {sound['artist']}")
        if used[1]:
            extra.append("trial reel")
        log(f"✅ posted {row['id']} → {res.get('permalink') or res['id']}"
            + (f" ({'; '.join(extra)})" if extra else ""), cfg)
        return True
    except instagram.IGRateLimited as e:
        log(f"⏳ {e} — will retry next slot", cfg)
    except Exception as e:
        attempts = (row["attempts"] or 0) + 1
        db.update(row["id"], attempts=attempts, error=str(e)[:500],
                  status="failed" if attempts >= 3 else row["status"])
        log(f"❌ posting {row['id']} failed ({attempts}/3): {e}", cfg)
    return False


def flagged_audio(cfg) -> bool:
    return reach.flagged(cfg, "audio_unavailable")


def _every(db: DB, job: str, minutes: float) -> bool:
    last = db.job_last_run(job)
    if not last:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(minutes=minutes)


def tick(cfg, db: DB, dry_run: bool = False, log=pipeline.log, due=None, process_inbox: bool = True) -> None:
    """One cycle. `due` overrides the posting rule (the iPhone uses catch-up slots)."""
    # 0. what's trending this week (daily, needs only the Claude key)
    tcfg = cfg.get("trends") or {}
    if tcfg.get("enabled", True) and cfg.ai_available and \
            _every(db, "trends", float(tcfg.get("refresh_hours", 24)) * 60):
        try:
            trends.refresh(cfg, log=lambda m: log(m, cfg))
        except Exception as ex:
            log(f"! trends: {ex}", cfg)
        db.job_done("trends")

    # 1. new recordings → reels
    for rec in (pipeline.new_recordings(cfg, db) if process_inbox else []):
        try:
            pipeline.process_file(cfg, db, rec)
        except Exception as e:
            log(f"❌ could not process {rec.name}: {e}", cfg)
            db.mark_source(rec.resolve(), "error", str(e)[:300])

    # 2. post if a slot is due
    connected = bool(cfg.ig_user_id and cfg.ig_token)
    if paused(cfg):
        return
    if (connected or dry_run) and (due or slot_due)(cfg, db):
        row = pick_next(cfg, db)
        if row:
            post(cfg, db, row, dry_run, log)
        else:
            log("📭 a posting slot is due but the queue is empty — drop more gameplay in the inbox", cfg)

    if dry_run or not connected:
        return
    e = cfg.get("engagement", {}) or {}
    ig = instagram.from_config(cfg)
    lg = lambda m: log(m, cfg)  # noqa: E731

    # 2b. read Instagram itself: top posts' hashtags, captions and posting hours (scout.py)
    tcfg = cfg.get("trends") or {}
    if tcfg.get("instagram_scout", True) and _every(db, "scout", float(tcfg.get("scout_every_hours", 48)) * 60):
        try:
            scout.refresh(cfg, ig, log=lg)
        except Exception as ex:
            log(f"! scout: {ex}", cfg)
        db.job_done("scout")

    # 3. answer comments (ig-reply)
    if e.get("reply_to_comments", True) and _every(db, "replies", float(e.get("reply_every_minutes", 45))):
        try:
            engagement.reply_round(cfg, db, ig, log=lg)
        except Exception as ex:
            log(f"! replies: {ex}", cfg)
        db.job_done("replies")

    # 4. stats → audit → learnings (ig-audit)
    if _every(db, "audit", float(e.get("audit_every_hours", 24)) * 60):
        try:
            engagement.refresh_insights(cfg, db, ig, log=lg)
            engagement.run_audit(cfg, db, log=lg)
        except Exception as ex:
            log(f"! audit: {ex}", cfg)
        db.job_done("audit")

    # 5. posting times from when followers are online (weekly)
    if cfg["schedule"].get("auto_times", True) and _every(db, "times", 7 * 1440):
        try:
            reach.update_times(cfg, ig, log=lg)
        except Exception as ex:
            log(f"! smart times: {ex}", cfg)
        db.job_done("times")

    # 6. profile check (ig-profile)
    if _every(db, "profile", float(e.get("profile_check_every_days", 30)) * 1440):
        try:
            engagement.profile_check(cfg, ig, log=lg)
        except Exception as ex:
            log(f"! profile check: {ex}", cfg)
        db.job_done("profile")


def watch(config_path=None, interval: int = 120, dry_run: bool = False) -> None:
    from .config import Config, load_env

    first = True
    while True:
        load_env()
        cfg = Config(config_path)          # re-read every cycle so dashboard changes apply live
        db = DB(cfg.state_dir / "agent.db")
        if first:
            pipeline.log(f"🤖 Reel Agent on autopilot. Inbox: {cfg.inbox}  |  posting at "
                         f"{', '.join(reach.post_times(cfg))} ({cfg.get('timezone')})", cfg)
            if not (cfg.ig_user_id and cfg.ig_token) and not dry_run:
                pipeline.log("⚠ Instagram not connected yet — reels will be made and queued, not posted.", cfg)
            first = False
        try:
            with JOB_LOCK:
                STATE["busy"] = "working"
                tick(cfg, db, dry_run)
        except KeyboardInterrupt:
            raise
        except Exception:
            pipeline.log("! unexpected error:\n" + traceback.format_exc(), cfg)
        finally:
            STATE["busy"] = ""
            STATE["last_tick"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.conn.close()
        WAKE.wait(interval)
        WAKE.clear()
