"""Which kind of reel to make from each clip, so the page stays creative instead of repeating one template.

The choice for every clip weighs four things:
  1. what the AI saw in the clip (it suggests a type from the stills)
  2. what is working on Instagram right now (scout.py: the kinds of reel among top posts, plus the trend brief)
  3. what has worked on THIS page (the daily audit: views of each type against the page's median)
  4. variety: a type used in the last few reels is marked down, and now and then a less-used type is
     tried on purpose so the page keeps learning (`clips.creativity`, 0 = never experiment, 1 = always)

Long recordings can also get one compilation (a top-3 countdown or a quick-cut montage) in place of the
weakest single reel.
"""
from __future__ import annotations

import json
import random

from . import editor

FORMATS = {
    "classic":  {"name": "Classic", "about": "the payoff flashes first, then the build-up, punch zoom, big hook",
                 "fits": "any strong moment"},
    "slowmo":   {"name": "Slow motion", "about": "the payoff plays at half speed",
                 "fits": "clean shots, goals, stunts, near misses"},
    "replay":   {"name": "Instant replay", "about": "the moment at full speed, then a slow-motion replay with a REPLAY tag",
                 "fits": "clutches, goals, anything worth a second look"},
    "freeze":   {"name": "Freeze frame", "about": "freezes just before the payoff with a label, then plays it out",
                 "fits": "fails, ambushes, 'he has no idea' moments"},
    "meme_bar": {"name": "Meme caption", "about": "white caption bar on top with a relatable line",
                 "fits": "funny moments, fails, relatable situations"},
    "pov":      {"name": "POV", "about": "a 'POV: ...' line in a caption box that stays up",
                 "fits": "first-person moments the viewer can put themselves in"},
    "top3":     {"name": "Top 3 countdown", "about": "three moments from one session counted down #3, #2, #1",
                 "fits": "recordings with three or more strong moments"},
    "montage":  {"name": "Montage", "about": "four to six quick cuts of the best moments",
                 "fits": "long sessions with lots of action"},
}
ON_REQUEST = {"highlight": {"name": "Highlight video", "about": "the critical parts of a whole session, in order"},
              "full": {"name": "Full video", "about": "the whole clip, lightly edited"}}
SINGLES = [f for f in FORMATS if f not in editor.COMPILATIONS]
NEEDS_LINE = {"meme_bar", "pov"}       # need a written line, so they need the AI

# what a kind of post seen on Instagram means for the types this editor can make
TREND_MAP = {
    "pov": {"pov": 1.0},
    "relatable": {"meme_bar": 0.7, "pov": 0.3},
    "wait_for_it": {"classic": 1.0},
    "rate": {"classic": 0.5, "replay": 0.5},
    "ranking": {"top3": 1.0},
    "clutch": {"replay": 0.6, "slowmo": 0.4},
    "fail": {"freeze": 0.6, "meme_bar": 0.4},
    "funny": {"meme_bar": 0.6, "freeze": 0.4},
    "edit": {"montage": 1.0},
    "satisfying": {"slowmo": 1.0},
}
END_CARD_TAGS = {"rate", "question"}   # kinds of post that end on a question


def _load(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def allowed(cfg) -> list[str]:
    c = cfg["clips"]
    fixed = c.get("style", "auto")
    if fixed != "auto" and fixed in FORMATS:
        return [fixed]
    return [f for f in (c.get("styles") or list(FORMATS)) if f in FORMATS] or ["classic"]


def trend_weights(cfg, game: str) -> dict[str, float]:
    """0..1 per type from what top posts look like right now (Instagram scout, YouTube Shorts, trend brief)."""
    w = {f: 0.0 for f in FORMATS}
    sc = _load(cfg.state_dir / "scout.json")
    yt = _load(cfg.state_dir / "youtube_study.json")
    seen = []
    for src in ((sc.get("games") or {}).get(game) or {}, (yt.get("games") or {}).get(game) or {}):
        seen += [(k["tag"], float(k.get("share", 0))) for k in src.get("formats", [])]
    for tag, share in seen:
        for fmt, part in TREND_MAP.get(tag, {}).items():
            w[fmt] += share * part
    brief = _load(cfg.state_dir / "trend_brief.json")
    rec = (brief.get("reel_types") or {}).get("recommended") or []
    if isinstance(rec, str):
        rec = [rec]
    old = (brief.get("edit_styles") or {}).get("recommended")
    for i, f in enumerate([r for r in rec if r in FORMATS][:4] or ([old] if old in FORMATS else [])):
        w[f] += max(0.25, 0.7 - 0.15 * i)
    top = max(w.values()) or 1.0
    return {f: round(v / top, 3) for f, v in w.items()}


def learned_weights(cfg) -> dict[str, float]:
    """Views of each type on this page against its median (1.0 = average). Only types with 2+ reels."""
    d = _load(cfg.state_dir / "learnings.json").get("style_weights") or {}
    return {k: float(v["x"]) for k, v in d.items() if k in FORMATS and v.get("n", 0) >= 2 and v.get("x")}


def recent_styles(db, n: int = 4) -> list[str]:
    rows = db.clips()[-n:]
    return [json.loads(r["render_json"] or "{}").get("style", "classic") for r in rows]


def scores(cfg, db, game: str, ai_pick: str | None, pool: list[str], used: list[str] | None = None) -> dict[str, float]:
    tw, lw = trend_weights(cfg, game), learned_weights(cfg)
    last = recent_styles(db, 4)          # includes the reels already made in this run
    out = {}
    for f in pool:
        s = 1.0
        if f == ai_pick:
            s += 1.2
        s += 0.6 * tw.get(f, 0.0)
        if f in lw:
            s += 0.8 * max(-0.5, min(1.0, lw[f] - 1.0))
        s -= 0.9 * last.count(f) + (0.4 if last and last[-1] == f else 0.0)
        out[f] = round(s, 3)
    return out


def choose(cfg, db, game: str, copy: dict, used: list[str] | None = None, seed: str = "") -> tuple[str, str]:
    """Pick the type for one clip. Returns (type, why)."""
    pool = [f for f in allowed(cfg) if f in SINGLES]
    if not (copy.get("meme_line") or "").strip():
        pool = [f for f in pool if f not in NEEDS_LINE]         # no written line: those types can't be made
    if copy.get("graphic_violence"):
        pool = [f for f in pool if f not in ("replay", "slowmo", "freeze")] or pool   # don't dwell on gore
    if not pool:
        return "classic", "only type available"
    if len(pool) == 1:
        return pool[0], "fixed in settings"
    ai_pick = copy.get("edit_style") if copy.get("edit_style") in pool else None
    sc = scores(cfg, db, game, ai_pick, pool, used)
    rng = random.Random(seed or None)
    creativity = float(cfg["clips"].get("creativity", 0.2))
    last = recent_styles(db, 3)
    fresh = [f for f in pool if f not in last]
    if fresh and rng.random() < creativity:
        pick = rng.choices(fresh, weights=[max(sc[f], 0.2) for f in fresh])[0]
        return pick, "trying a type the page hasn't used lately"
    pick = max(pool, key=lambda f: (sc[f], -pool.index(f)))
    tw = trend_weights(cfg, game)
    lw = learned_weights(cfg)
    if pick == ai_pick:
        why = "fits what happens in the clip"
    elif lw.get(pick, 0) > 1.15:
        why = f"this type gets {lw[pick]:.1f}x your usual views"
    elif tw.get(pick, 0) >= 0.6:
        why = "common among top posts right now"
    else:
        why = "keeps the page varied"
    return pick, why


def compilation(cfg, db, game: str, n_moments: int, duration: float, want: int, seed: str = "") -> str | None:
    """Should this recording also get a top-3 or montage (replacing the weakest single reel)?"""
    pool = [f for f in allowed(cfg) if f in editor.COMPILATIONS and n_moments >= editor.COMPILATIONS[f]]
    if not pool:
        return None
    if allowed(cfg) == pool:                       # the owner fixed the style to a compilation
        return pool[0]
    if not (duration >= 180 or n_moments >= want + 2):
        return None                                # short clip: single reels are the better use of it
    recent = recent_styles(db, 5)
    if any(s in editor.COMPILATIONS for s in recent[-3:]):
        return None                                # one compilation every few reels at most
    sc = scores(cfg, db, game, None, pool)
    rng = random.Random(seed or None)
    best = max(pool, key=lambda f: sc[f])
    chance = 0.45 + 0.25 * trend_weights(cfg, game).get(best, 0.0) + float(cfg["clips"].get("creativity", 0.2)) * 0.5
    return best if rng.random() < min(chance, 0.9) else None


def end_card(cfg, game: str, copy: dict, profile: dict, seed: str = "") -> str:
    """A closing question for the last second, when the AI wrote one or top posts lean on them."""
    if not cfg["clips"].get("end_cards", True) or copy.get("graphic_violence"):
        return ""
    text = " ".join(str(copy.get("end_card") or "").split()[:5]).strip()
    rng = random.Random(seed or None)
    sc = (_load(cfg.state_dir / "scout.json").get("games") or {}).get(game) or {}
    share = sum(float(k.get("share", 0)) for k in sc.get("formats", []) if k.get("tag") in END_CARD_TAGS)
    chance = min(0.75, 0.35 + share)                 # not on every reel: it would become wallpaper
    if rng.random() > chance:
        return ""
    if text:
        return text
    bank = profile.get("end_cards") or ["Rate this /10", "W or L?", "Clutch or luck?"]
    return rng.choice(bank)


def reel_length(cfg, game: str) -> float:
    """Seconds per reel: the configured length, pulled toward what top reels in this niche run at."""
    base = float(cfg["clips"]["length"])
    if not cfg["clips"].get("auto_length", True):
        return base
    meds = []
    for name in ("scout.json", "youtube_study.json"):
        g = (_load(cfg.state_dir / name).get("games") or {}).get(game) or {}
        m = (g.get("playbook") or {}).get("median_seconds") or g.get("median_seconds")
        if m:
            meds.append(float(m))
    if not meds:
        return base
    target = max(12.0, min(32.0, sum(meds) / len(meds)))
    return round((base + target) / 2, 1)


def cut_seconds(cfg, game: str) -> float:
    g = (_load(cfg.state_dir / "scout.json").get("games") or {}).get(game) or {}
    shot = (g.get("playbook") or {}).get("median_shot_seconds")
    return max(1.2, min(3.0, float(shot))) if shot else 2.0


def menu(pool: list[str]) -> str:
    return "\n".join(f"- {f}: {FORMATS[f]['about']}. Fits: {FORMATS[f]['fits']}." for f in pool if f in FORMATS)
