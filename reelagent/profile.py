"""Your Instagram profile, kept follow-worthy.

Instagram's API reads a profile but has no way to change one: no app can edit the name field, bio,
link, photo, pinned reels or highlights. So the agent does all the thinking and leaves the pasting:

  1. every few hours it reads the profile (one cheap API call)
  2. when the profile is new to it, has changed, or the last write-up is two weeks old, it scores it
     against the ig-profile rubric and writes the fixes: three name-field options (the field Instagram
     search matches), three bios, which reels to pin, highlight names
  3. without the AI (or when Google's free AI is busy) it writes the same fixes from templates, and
     tries the AI again a few hours later
  4. status.json carries the result; the app shows each fix with a copy button

When the owner pastes a fix in, the next read sees the change and scores the profile again.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

NAME_MAX, BIO_MAX = 30, 150
READ_EVERY_H = 6                 # how often the profile is read
REWRITE_DAYS = 14                # a fresh write-up at least this often
AI_RETRY_H = 3                   # after a template fallback, try the AI again this soon

SHORT = {"gta6": "GTA 6", "gta": "GTA", "cod": "COD", "warzone": "Warzone", "fc27": "FC 27", "fc26": "FC 26",
         "fortnite": "Fortnite", "valorant": "Valorant", "apex": "Apex", "minecraft": "Minecraft"}
TAG = {"gta6": "#GTA6", "cod": "#CallOfDuty", "fc27": "#EAFC27", "fortnite": "#Fortnite", "valorant": "#Valorant"}
WORDS = ("gaming", "gamer", "games", "clipz", "clips", "clip", "plays", "highlights", "moments", "official",
         "tv", "hub", "zone", "daily")
RUBRIC_LABELS = {"name_field": "Name field", "bio_first_line": "Bio, first line", "bio_body": "Bio, the rest",
                 "pinned_three": "Pinned reels", "link": "Link", "highlights": "Highlights",
                 "grid_legibility": "Grid covers", "photo": "Profile photo", "handle": "Handle",
                 "category_contact": "Category and contact", "recent_activity": "Posting lately",
                 "story_presence": "Stories"}


def path(cfg) -> Path:
    return cfg.state_dir / "profile.json"


def flag(cfg) -> Path:
    return cfg.state_dir / "profile_refresh"


def load(cfg) -> dict:
    try:
        return json.loads(path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _age_h(iso: str | None) -> float:
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except Exception:
        return 1e9


def ig_len(s: str) -> int:
    """Characters as Instagram counts them (UTF-16 units, so most emoji count twice)."""
    return len(s.encode("utf-16-le")) // 2


def _tidy(s: str, limit: int) -> str | None:
    s = str(s or "").replace("\r", "").replace("\\n", "\n")
    s = re.sub(r"\s*—\s*", ", ", s).replace("–", "-")
    s = "\n".join(" ".join(line.split()) for line in s.split("\n")).strip()
    s = re.sub(r"\n{2,}", "\n", s)
    return s if s and ig_len(s) <= limit else None


# ── the page's facts ────────────────────────────────────────────────────────

def games(cfg) -> list[tuple[str, str, str]]:
    """(key, short name, full name) for each game the page posts."""
    out = []
    for k, g in (cfg.get("games") or {}).items():
        if k == "default" or not isinstance(g, dict):
            continue
        full = str(g.get("name") or k)
        short = SHORT.get(k) or re.sub(r"^EA Sports\s+", "", full).replace("Call of Duty", "COD")
        out.append((k, short, full))
    return out


def brand(username: str) -> str:
    """average_gamingclipz -> Average Gaming Clipz"""
    s = re.sub(r"[._]+", " ", username or "").strip()
    for w in WORDS:
        s = re.sub(rf"(?i)(?<=[a-z])({w})\b", r" \1", s)
    return " ".join(p[:1].upper() + p[1:] for p in s.split())


def _join(names: list[str], last: str = "&") -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" {last} " + names[-1]


def keywords(cfg) -> list[str]:
    ks = []
    for k, short, full in games(cfg):
        ks += [short, full, k]
    return [x.lower() for x in ks if x] + ["clip", "gaming", "gameplay"]


def top_reels(cfg, db, n: int = 3) -> list[dict]:
    rows = db.conn.execute(
        "SELECT c.id, c.copy_json, c.posted_at, c.ig_url, i.views, i.shares FROM clips c "
        "LEFT JOIN insights i ON i.clip_id = c.id WHERE c.status = 'posted'").fetchall()
    out = []
    for r in rows:
        try:
            copy = json.loads(r["copy_json"] or "{}")
        except Exception:
            copy = {}
        out.append({"id": r["id"], "hook": copy.get("hook_text") or r["id"], "views": r["views"] or 0,
                    "shares": r["shares"] or 0, "link": r["ig_url"], "at": r["posted_at"]})
    out.sort(key=lambda x: (x["views"] + 20 * x["shares"]), reverse=True)
    return out[:n]


# ── fixes without the AI ────────────────────────────────────────────────────

def template_kit(cfg, prof: dict, posted: int, top: list[dict]) -> dict:
    gs = games(cfg)
    shorts = [s for _, s, _ in gs] or ["Gaming"]
    fulls = [f for _, _, f in gs] or ["gaming"]
    b = brand(prof.get("username") or cfg.user_config().get("ig_username") or "")
    names = [f"{' • '.join(shorts[:3])} Clips", f"Gaming Clips | {_join(shorts[:2])}",
             f"{b} | {shorts[0]}" if b else "", f"{shorts[0]} & {shorts[1]} Clips" if len(shorts) > 1 else ""]
    tags = " ".join([TAG[k] for k, _, _ in gs if k in TAG][:2])
    avg = "average" in (prof.get("username") or "").lower()
    bios = [
        f"Daily {_join(shorts[:3])} clips 🎮\nThe kills, fails and moments worth a replay.\nTurn on notifications so you catch the next one 🔔",
        (f"Average gamer, above average clips 🎮\n{_join(shorts[:3])} every day.\n{tags}".strip() if avg else
         f"{shorts[0]} clips before everyone else 🔥\nPlus {_join(shorts[1:3]) or 'more'} highlights, 30 seconds each.\n{tags}".strip()),
        f"The best {_join(fulls[:2], 'and')} moments, cut short 🎯\nRate every clip in the comments 👇",
    ]
    return {"name_options": [x for x in (_tidy(n, NAME_MAX) for n in names) if x][:3],
            "bio_options": [x for x in (_tidy(t, BIO_MAX) for t in bios) if x][:3],
            "highlights": shorts[:3] + ["Best of", "Fails"],
            "link": "Leave it empty for now. When the YouTube channel is running, put that one link here.",
            "summary": "", "score": None, "items": [], "after_score": None}


# ── fixes with the AI ───────────────────────────────────────────────────────

AI_RULES = """
You are scoring and rewriting the profile of an Instagram page that posts short gameplay clips.
Nothing you write is saved to Instagram automatically: the owner pastes it in, so every rewrite must be
ready to paste exactly as written.
Rules:
- name_options: 3 options for the NAME FIELD (the bold line, not the handle), each at most 30 characters
  including spaces. Put the words people search for: the games and "clips". Brand name optional.
- bio_options: 3 bios, each at most 150 characters (emoji count as 2). Line 1 says who it's for and what
  they get. Plain sentences, no pipe-separated lists, no em dashes, at most 2 emoji, at most 2 hashtags
  (hashtags in a bio are clickable, use the game's main one). Use \\n for line breaks (at most 3 lines).
- Never invent numbers, followers, giveaways, prizes, schedules the facts don't support, or links.
- Score honestly against the rubric. A new page with an empty name field and bio scores in the teens.
- You can't see the photo, the grid or the stories. Score those only from the facts given (has_photo,
  reels_posted); never describe what a photo looks like.
- items: one per rubric id: {"id": "<rubric id>", "got": <points>, "of": <max>, "note": "<= 12 words"}.
- highlights: 4 to 6 short highlight names (1-2 words each) for later, when stories exist.
- link: one sentence on where the single bio link should point (or to leave it empty), no URLs invented.
- after_score: honest estimate if the owner applies your name, bio and pins.
Return ONLY JSON: {"score": n, "summary": "<one plain sentence on the biggest gap>", "items": [...],
"name_options": [...], "bio_options": [...], "highlights": [...], "link": "...", "after_score": n}
"""


def ai_kit(cfg, prof: dict, facts: dict) -> dict:
    from . import toolkit
    from .captions import ask_claude, parse_json
    system = toolkit.playbook("ig-profile") + "\n\n" + AI_RULES
    prompt = (f"Profile, read from Instagram just now:\n{json.dumps(prof, ensure_ascii=False)}\n\n"
              f"Facts about the page:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
              f"Rubric:\n{json.dumps(toolkit.profile_rubric(), ensure_ascii=False)}")
    data = parse_json(ask_claude(cfg, system, [{"type": "text", "text": prompt}], 2200))
    if not isinstance(data, dict):
        raise ValueError("the AI's answer wasn't the expected JSON")
    rubric = {i["id"]: i["points"] for i in toolkit.profile_rubric()["items"]}
    items = []
    for it in data.get("items") or []:
        if isinstance(it, dict) and it.get("id") in rubric:
            of = rubric[it["id"]]
            got = max(0, min(of, int(float(it.get("got") or 0))))
            items.append({"id": it["id"], "label": RUBRIC_LABELS.get(it["id"], it["id"]), "got": got, "of": of,
                          "note": " ".join(str(it.get("note") or "").split()[:16])})
    score = sum(i["got"] for i in items) if len(items) >= 8 else None
    return {"name_options": [x for x in (_tidy(n, NAME_MAX) for n in data.get("name_options") or []) if x][:3],
            "bio_options": [x for x in (_tidy(t, BIO_MAX) for t in data.get("bio_options") or []) if x][:3],
            "highlights": [" ".join(str(h).split()[:2]) for h in (data.get("highlights") or []) if str(h).strip()][:6],
            "link": " ".join(str(data.get("link") or "").split())[:200],
            "summary": " ".join(str(data.get("summary") or "").split())[:220],
            "score": score if score is not None else _pct(data.get("score")),
            "items": items, "after_score": _pct(data.get("after_score"))}


def _pct(v) -> int | None:
    try:
        return max(0, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


# ── what still needs doing ──────────────────────────────────────────────────

def fixes(cfg, prof: dict, kit: dict, posted: int, top: list[dict]) -> list[dict]:
    keys = keywords(cfg)
    has = lambda s: any(k in (s or "").lower() for k in keys)  # noqa: E731
    name, bio, site = prof.get("name") or "", prof.get("biography") or "", prof.get("website") or ""
    out = [{
        "id": "name", "title": "Name field", "now": name,
        "done": bool(name) and has(name),
        "why": "The bold line under your photo. Instagram search matches it, so it should say what the page is. "
               "It's set in Edit profile → Name. Instagram lets you change it twice in 14 days.",
        "options": kit.get("name_options") or []},
        {"id": "bio", "title": "Bio", "now": bio,
         "done": bool(bio) and has(bio),
         "why": "A stranger arrives from one reel and decides in about three seconds. The bio tells them there's "
                "more of that here. Edit profile → Bio, 150 characters.",
         "options": kit.get("bio_options") or []}]
    if posted >= 3:
        out.append({"id": "pin", "title": "Pin your best reels", "done": False, "manual": True,
                    "why": "Pinned reels sit at the top of your grid, so new visitors see your best work first. "
                           "Open the reel in Instagram, tap ⋯, then Pin to your profile. Up to 3.",
                    "reels": top})
    else:
        out.append({"id": "pin", "title": "Pin your best reels", "done": False, "later": True,
                    "why": "Once 3 reels are up, the agent tells you which ones to pin here."})
    out.append({"id": "link", "title": "Link", "done": bool(site), "optional": True, "now": site,
                "why": kit.get("link") or "One link at most, to where your clips continue (a YouTube channel)."})
    return out


# ── the job ─────────────────────────────────────────────────────────────────

def job(cfg, db, ig, log=print, force: bool = False) -> dict | None:
    """Read the profile and refresh the write-up when it is due. Returns the kit, or None if not run."""
    old = load(cfg)
    force = force or flag(cfg).exists()
    if not force and _age_h(old.get("read_at")) < READ_EVERY_H:
        return None
    flag(cfg).unlink(missing_ok=True)
    raw = ig.profile()
    prof = {k: raw.get(k) for k in ("username", "name", "biography", "website", "followers_count",
                                     "follows_count", "media_count")}
    has_photo = bool(raw.get("profile_picture_url"))
    sig = json.dumps([prof.get("name"), prof.get("biography"), prof.get("website")], ensure_ascii=False)
    posted = db.conn.execute("SELECT COUNT(*) FROM clips WHERE status = 'posted'").fetchone()[0]
    top = top_reels(cfg, db)
    changed = bool(old) and old.get("sig") != sig
    stale = _age_h(old.get("written_at")) >= 24 * float((cfg.get("engagement") or {}).get("profile_check_every_days", REWRITE_DAYS))
    retry_ai = old.get("source") == "template" and cfg.ai_available and _age_h(old.get("written_at")) >= AI_RETRY_H
    pins_new = [r["id"] for r in top] != [r.get("id") for r in (old.get("top") or [])]
    kit = dict(old)
    if force or not old or changed or stale or retry_ai or "name_options" not in old:
        kit = template_kit(cfg, prof, posted, top)
        kit["source"] = "template"
        if cfg.ai_available:
            facts = {"games": [f for _, _, f in games(cfg)], "reels_posted": posted,
                     "reels_per_day": int(cfg["schedule"]["max_per_day"]),
                     "kinds_of_reel": list((cfg.get("clips") or {}).get("styles") or [])[:8],
                     "best_reels": [{"hook": r["hook"], "views": r["views"]} for r in top],
                     "has_photo": has_photo,
                     "account_type": "Instagram Creator account, category Gamer, linked Facebook Page"}
            try:
                got = ai_kit(cfg, prof, facts)
                for k, v in got.items():
                    if k in ("name_options", "bio_options"):          # the AI's first, topped up to three
                        v = list(dict.fromkeys(v + kit.get(k, [])))[:3]
                    if v not in (None, [], ""):
                        kit[k] = v
                kit["source"] = "ai"
            except Exception as e:
                log(f"(profile: the AI is busy, used the built-in write-up and will try again later: {str(e)[:120]})")
        kit["written_at"] = _iso(_now())
        what = "changed, scored it again" if changed else "written"
        score = f" {kit['score']}/100" if kit.get("score") is not None else ""
        todo = len([f for f in fixes(cfg, prof, kit, posted, top) if not (f["done"] or f.get("later") or f.get("optional"))])
        log(f"🪪 profile check {what}:{score} " + (f"({todo} fix{'es' if todo != 1 else ''} waiting in the app)" if todo else "(nothing to fix)"))
    elif pins_new:
        log("🪪 profile: new best reels to pin")
    kit.update({"read_at": _iso(_now()), "sig": sig, "current": prof, "top": top, "posted": posted,
                "username": prof.get("username") or ""})
    kit["fixes"] = fixes(cfg, prof, kit, posted, top)
    path(cfg).write_text(json.dumps(kit, indent=1, ensure_ascii=False), encoding="utf-8")
    return kit


def for_status(cfg) -> dict | None:
    k = load(cfg)
    if not k.get("fixes"):
        return None
    return {key: k.get(key) for key in ("username", "current", "score", "after_score", "summary", "items",
                                        "fixes", "highlights", "source", "written_at", "read_at")}
