"""Reach boosters applied at posting time.

  sound   : picks a trending Instagram sound that fits the clip (Audio API) and attaches it to the reel.
            Candidates = what's trending now + searches for the trend brief's music terms + the clip's mood;
            Claude picks the best fit (or none). Recently used sounds are skipped.
  trial   : trial reels go to non-followers first and are shared to followers automatically if they
            perform (graduation_strategy SS_PERFORMANCE). "smart" uses them for lower-confidence reels.
  times   : once the page has 100+ followers, posting times follow when your followers are online.
State that must survive restarts (e.g. "this account can't do trial reels yet") lives in state/flags.json.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import trends

MOOD_TERMS = {"hype": ["hype"], "intense": ["phonk"], "epic": ["epic"], "chill": ["lofi"],
              "funny": ["funny"], "suspense": ["suspense"]}


# ── flags ───────────────────────────────────────────────────────────────────

def _flags_path(cfg):
    return cfg.state_dir / "flags.json"


def flags(cfg) -> dict:
    try:
        return json.loads(_flags_path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return {}


def set_flag(cfg, key: str, days: float) -> None:
    f = flags(cfg)
    f[key] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    _flags_path(cfg).write_text(json.dumps(f, indent=2), encoding="utf-8")


def flagged(cfg, key: str) -> bool:
    until = flags(cfg).get(key)
    return bool(until) and datetime.fromisoformat(until) > datetime.now(timezone.utc)


# ── trending Instagram sound ────────────────────────────────────────────────

def _cached_trending(cfg, ig) -> list[dict]:
    p = cfg.state_dir / "ig_trending_audio.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - d["at"] < 2 * 3600:
            return d["items"]
    except Exception:
        pass
    items = ig.search_audio(None, limit=25)
    p.write_text(json.dumps({"at": time.time(), "items": items}, ensure_ascii=False), encoding="utf-8")
    return items


LAST = {"why": ""}     # why the last plan_sound found nothing: "empty" (Instagram offered none) or "no_fit"


def plan_sound(cfg, db, copy: dict, game: str, exclude: list[str] | None = None, log=print) -> dict | None:
    """Choose the Instagram sound for a reel while it's being edited, so the owner sees it before approving.
    None when Instagram isn't connected, sounds are switched off, or nothing fits (LAST["why"] says which)."""
    LAST["why"], LAST["options"] = "no_fit", []
    from . import instagram
    from .db import copy_of
    igc = cfg.get("instagram") or {}
    if not (igc.get("trending_audio", True) and cfg.ig_user_id and cfg.ig_token) or flagged(cfg, "audio_unavailable"):
        return None
    recent = [(copy_of(r).get("ig_audio") or copy_of(r).get("ig_audio_plan") or {}).get("audio_id")
              for r in db.clips()[-8:]]
    try:
        return pick_sound(cfg, instagram.from_config(cfg), copy, game, [x for x in recent + list(exclude or []) if x], log=log)
    except Exception as e:
        log(f"  (no Instagram sound: {e})")
        return None


def pick_sound(cfg, ig, copy: dict, game: str, recent_ids: list[str], log=print) -> dict | None:
    cands, seen = [], set(recent_ids)

    def add(items, source):
        for it in items:
            aid = str(it.get("audio_id") or it.get("id") or "")
            if not aid or aid in seen or (it.get("duration_in_ms") or 30000) < 8000:
                continue
            seen.add(aid)
            cands.append({"audio_id": aid, "title": it.get("title", ""), "artist": it.get("display_artist", ""),
                          "source": source, "link": it.get("on_platform_audio_preview_link") or ""})

    try:
        add(_cached_trending(cfg, ig), "trending now")
    except Exception as e:
        log(f"  (trending sounds unavailable: {e})")
    for term in (trends.music_terms(cfg)[:3] + MOOD_TERMS.get(copy.get("music_mood", ""), []))[:4]:
        try:
            add(ig.search_audio(term, limit=8), f"search: {term}")
        except Exception as e:
            log(f"  (Instagram music search '{term}' failed: {safety_text(e)[:160]})")
    if not cands:                      # no cleared music: trending original sounds are Instagram audio too
        try:
            add([{**i, "display_artist": i.get("display_artist") or ("@" + i["ig_username"] if i.get("ig_username") else "")}
                 for i in ig.search_audio(None, audio_type="original_sound", limit=25)], "trending original sound")
        except Exception as e:
            log(f"  (trending original sounds unavailable: {safety_text(e)[:160]})")
    if not cands:
        log("  (Instagram's Audio API returned no sounds for this account)")
        LAST["why"] = "empty"
        return None
    cands = cands[:30]
    if not cfg.ai_available:
        LAST["options"] = cands[:3]
        return cands[0]
    from .captions import ask_claude, parse_json

    menu = "\n".join(f"{i}. {c['title']} - {c['artist']} [{c['source']}]" for i, c in enumerate(cands))
    prompt = (f"Pick background music for an Instagram Reel of {cfg.game_profile(game).get('name')} gameplay.\n"
              f"Clip: {copy.get('what_happens') or 'gameplay highlight'}; mood: {copy.get('music_mood')}; "
              f"on-screen hook: {copy.get('hook_text')}.\nTrending music styles this week: "
              f"{', '.join((trends.load(cfg).get('music') or {}).get('genres', [])) or 'unknown'}.\n"
              "Prefer a sound that is trending now AND fits the energy. Pick none if nothing fits "
              "(a mismatched song hurts watch time).\n"
              f"Options:\n{menu}\nReply JSON: {{\"index\": <number or null>, \"why\": \"...\"}}")
    try:
        choice = parse_json(ask_claude(cfg, "You pick music for short gaming videos.", [{"type": "text", "text": prompt}], 200))
        i = choice.get("index")
        pick = cands[int(i)] if i is not None and 0 <= int(i) < len(cands) else None
    except Exception:
        pick = cands[0]
    LAST["options"] = ([pick] if pick else []) + [c for c in cands if c is not pick][:3 - bool(pick)]
    return pick


# ── what Instagram's Audio API offers this account ──────────────────────────

def audio_check(cfg, ig, log=print) -> dict:
    """Ask the Audio API for trending music, trending original sounds and one search, and keep what came
    back (counts, a few titles, errors). Instagram only offers apps the audio it has cleared for third-party
    use, and the selection differs by account and country, so this is the only way to know."""
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for key, kind, q in (("music_trending", "music", None), ("original_trending", "original_sound", None),
                         ("music_search", "music", "hype")):
        try:
            items = ig.search_audio(q, audio_type=kind, limit=25)
            out[key] = {"n": len(items), "titles": [f"{i.get('title', '')} - {i.get('display_artist') or i.get('ig_username') or ''}".strip(" -")
                                                     for i in items[:5]]}
        except Exception as e:
            out[key] = {"n": 0, "error": safety_text(e)}
    (cfg.state_dir / "audio_check.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    summary = ", ".join(f"{k.replace('_', ' ')}: {v.get('n', 0)}" + (f" (error: {v['error'][:90]})" if v.get("error") else "")
                        for k, v in out.items() if isinstance(v, dict))
    log(f"🎵 Instagram's music for apps: {summary}")
    return out


def safety_text(e) -> str:
    from . import safety
    return safety.scrub(str(e))[:300]


# ── trial reels ─────────────────────────────────────────────────────────────

def trial_strategy(cfg, row) -> str | None:
    mode = (cfg.get("instagram") or {}).get("trial_reels", "smart")
    if mode == "off" or flagged(cfg, "trial_unavailable"):
        return None
    if mode == "always":
        return "SS_PERFORMANCE"
    return "SS_PERFORMANCE" if (row["quality"] or 0) < 8 else None   # smart: test the less certain ones


# ── posting times from audience activity ───────────────────────────────────

def post_times(cfg) -> list[str]:
    sch = cfg["schedule"]
    if sch.get("auto_times", True):
        try:
            d = json.loads((cfg.state_dir / "smart_times.json").read_text(encoding="utf-8"))
            if d.get("times") and time.time() - d.get("at", 0) < 21 * 86400:
                return d["times"]
        except Exception:
            pass
        from . import scout   # before 100 followers: when the top posts in your niche go live
        st = scout.post_times(cfg)
        if st:
            return st
    return [str(t) for t in sch["post_times"]]


def update_times(cfg, ig, log=print) -> list[str] | None:
    hours = ig.online_followers()
    if not hours:
        log("🕒 smart post times: not available yet (Instagram shares this from 100 followers)")
        return None
    src = ZoneInfo(cfg["schedule"].get("online_followers_tz", "America/Los_Angeles"))
    local = ZoneInfo(cfg.get("timezone", "America/Toronto"))
    today = date.today()
    score = {}
    for h, v in hours.items():
        lt = datetime.combine(today, dtime(h % 24, 0), tzinfo=src).astimezone(local)
        score[lt.hour] = v
    want = int(cfg["schedule"]["max_per_day"])
    chosen = []
    for h in sorted(score, key=score.get, reverse=True):
        if 10 <= h <= 23 and all(abs(h - c) >= 3 for c in chosen):
            chosen.append(h)
        if len(chosen) >= want:
            break
    if not chosen:
        return None
    times = [f"{max(h - 1, 9):02d}:40" for h in sorted(chosen)]   # go live just before followers peak
    (cfg.state_dir / "smart_times.json").write_text(json.dumps(
        {"at": time.time(), "times": times, "peak_hours_local": sorted(chosen)}, indent=2), encoding="utf-8")
    log(f"🕒 smart post times from follower activity: {', '.join(times)}")
    return times
