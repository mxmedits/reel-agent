"""Reads Instagram itself: what captions, hashtags and posting times are working on top posts right now.

Every couple of days (all free, through your own Instagram connection):
  1. Hashtag Search → the TOP posts for each game's core hashtags (Instagram's own "Top" ranking),
     plus a look at the rising hashtags found on them. Instagram allows 30 unique hashtags per
     7 days per account; the scout keeps to `trends.hashtag_budget` (re-checking a tag is free).
  2. Business Discovery → recent posts of benchmark pages you list in config (trends.benchmark_pages).
  3. From those posts it builds, per game:
       • trending hashtags   ranked by how often they appear on top posts, weighted by engagement
       • caption patterns    the opening lines of the best posts, typical length, questions, CTAs, emoji
       • best posting hours  when the top posts went live (in your time zone), engagement-weighted
Saved to output/state/scout.json and used by the caption writer (hashtags + caption style), the
scheduler (posting times until Instagram shares your own followers' online hours at 100 followers)
and the daily trend brief. If Meta hasn't enabled hashtag search for your app yet, it says so once and
the agent carries on with web + YouTube trend data.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# hashtags that do nothing for reach (or flag a post as spam): never recommended
SPAMMY = {
    "fyp", "foryou", "foryoupage", "explore", "explorepage", "viral", "viralvideo", "viralreels", "reels",
    "reel", "reelsinstagram", "instagood", "instagram", "instadaily", "follow", "followme", "follow4follow",
    "followforfollow", "f4f", "like", "likes", "like4like", "l4l", "likeforlike", "likeforlikes", "tagsforlikes",
    "photooftheday", "trending", "trend", "trendingreels", "explorer", "fypage", "fypシ", "instareels",
    "comment4comment", "c4c", "followback", "shoutout", "s4s", "love", "tiktok", "youtube", "shorts", "youtubeshorts",
}
CTA_WORDS = ("comment", "tag", "send", "share", "follow", "save", "rate", "drop", "tell me", "who", "would you")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
TAG = re.compile(r"#(\w{2,40})", re.U)


def path(cfg) -> Path:
    return cfg.state_dir / "scout.json"


def load(cfg) -> dict:
    try:
        return json.loads(path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _budget_file(cfg) -> Path:
    return cfg.state_dir / "hashtag_budget.json"


def _budget(cfg) -> dict:
    """{tag: last_queried_epoch} for the rolling 7-day window Instagram enforces."""
    try:
        d = json.loads(_budget_file(cfg).read_text(encoding="utf-8"))
    except Exception:
        d = {}
    week = time.time() - 7 * 86400
    return {k: v for k, v in d.items() if v > week}


def _spend(cfg, used: dict, tag: str) -> None:
    used[tag] = time.time() if tag not in used else used[tag]
    _budget_file(cfg).write_text(json.dumps(used, indent=1), encoding="utf-8")


def _engagement(m: dict) -> float:
    return float(m.get("like_count") or 0) + 4.0 * float(m.get("comments_count") or 0)


def _first_line(caption: str) -> str:
    for line in (caption or "").splitlines():
        line = TAG.sub("", line).strip()
        if len(line) > 3:
            return line[:140]
    return ""


def analyse(posts: list[dict], tz: ZoneInfo, exclude: set[str] = frozenset(),
            other_games: set[str] = frozenset()) -> dict:
    """Turn a list of IG media dicts into hashtag ranks, caption patterns and hour weights."""
    posts = [p for p in posts if p.get("caption") or p.get("timestamp")]
    if not posts:
        return {}
    tag_score, tag_count = defaultdict(float), Counter()
    hours = [0.0] * 24
    lengths, questions, ctas, emojis, ntags = [], 0, 0, 0, []
    for p in posts:
        w = math.log1p(_engagement(p)) + 0.5
        cap = p.get("caption") or ""
        tags = {t.lower() for t in TAG.findall(cap)}
        ntags.append(len(tags))
        for t in tags:
            if t in SPAMMY or t in exclude or t in other_games:   # keep each game's tags its own
                continue
            tag_score[t] += w
            tag_count[t] += 1
        body = TAG.sub("", cap).strip()
        if body:
            lengths.append(len(body))
            questions += "?" in body
            ctas += any(c in body.lower() for c in CTA_WORDS)
            emojis += bool(EMOJI.search(body))
        ts = p.get("timestamp")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace("+0000", "+00:00"))
                hours[dt.astimezone(tz).hour] += w
            except ValueError:
                pass
    n = len(posts)
    ranked = sorted(tag_score, key=lambda t: (tag_score[t], tag_count[t]), reverse=True)
    best = sorted(posts, key=_engagement, reverse=True)
    lines, seen = [], set()
    for p in best:
        fl = _first_line(p.get("caption") or "")
        if fl and fl.lower() not in seen:
            seen.add(fl.lower())
            lines.append({"line": fl, "likes": p.get("like_count"), "comments": p.get("comments_count")})
        if len(lines) >= 8:
            break
    return {
        "posts": n,
        "hashtags": [{"tag": "#" + t, "on_posts": tag_count[t], "score": round(tag_score[t], 1)}
                     for t in ranked[:20] if tag_count[t] >= 2 or n < 12],
        "top_captions": lines,
        "caption_stats": {
            "median_length": int(statistics.median(lengths)) if lengths else 0,
            "with_question": round(questions / max(len(lengths), 1), 2),
            "with_cta": round(ctas / max(len(lengths), 1), 2),
            "with_emoji": round(emojis / max(len(lengths), 1), 2),
            "median_hashtags": int(statistics.median(ntags)) if ntags else 0,
        },
        "hour_weights": [round(h, 2) for h in hours],
    }


def best_hours(weights: list[float], want: int, earliest: int = 9, latest: int = 23) -> list[int]:
    if not weights or not any(weights):
        return []
    sm = [weights[(h - 1) % 24] * 0.25 + weights[h] * 0.5 + weights[(h + 1) % 24] * 0.25 for h in range(24)]
    chosen = []
    for h in sorted(range(24), key=lambda x: sm[x], reverse=True):
        if earliest <= h <= latest and sm[h] > 0 and all(abs(h - c) >= 3 for c in chosen):
            chosen.append(h)
        if len(chosen) >= want:
            break
    return sorted(chosen)


def refresh(cfg, ig, log=print) -> dict:
    """Pull top posts for each game's hashtags (+ benchmark pages), analyse and save. Returns the scout data."""
    tcfg = cfg.get("trends") or {}
    if not tcfg.get("instagram_scout", True):
        return load(cfg)
    tz = ZoneInfo(cfg.get("timezone", "America/Toronto"))
    budget_cap = int(tcfg.get("hashtag_budget", 24))
    used = _budget(cfg)
    fields = "id,caption,like_count,comments_count,media_type,permalink,timestamp"
    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "games": {}, "notes": []}
    all_posts: list[dict] = []
    hashtag_ok = True

    def query(tag: str) -> list[dict]:
        nonlocal hashtag_ok
        tag = tag.lstrip("#").lower()
        if not hashtag_ok:
            return []
        if tag not in used and len(used) >= budget_cap:
            out["notes"].append(f"skipped #{tag}: weekly hashtag budget used ({budget_cap}/30)")
            return []
        try:
            hid = ig.hashtag_id(tag)
            if not hid:
                return []
            _spend(cfg, used, tag)
            return [p for p in ig.hashtag_media(hid, "top_media", fields, pages=2)
                    if p.get("media_type") in (None, "VIDEO")]
        except Exception as e:
            hashtag_ok = False
            msg = str(e)[:160]
            out["notes"].append(f"hashtag search unavailable: {msg}")
            log(f"  (Instagram hashtag search not available for this app yet: {msg})")
            return []

    def tags_of(k: str, p: dict) -> set[str]:
        return {t.lstrip("#").lower() for t in (p.get("hashtags") or [])} | {k.lower()}

    generic = {"gaming", "gamer", "gamingclips", "gameplay", "clips", "videogames", "gamers"}
    for key, prof in cfg["games"].items():
        if key == "default":
            continue
        others = set().union(*[tags_of(k, p) for k, p in cfg["games"].items() if k not in (key, "default")]) \
            - tags_of(key, prof) - generic
        core = [t.lstrip("#") for t in (prof.get("scout_tags") or prof.get("hashtags", [])[:2])]
        posts = []
        for t in core:
            posts += query(t)
        exclude = {t.lower() for t in core}
        res = analyse(posts, tz, other_games=others)
        # dig one level deeper: the strongest rising tag on those top posts
        explore = int(tcfg.get("explore_tags_per_game", 1))
        for h in (res.get("hashtags") or [])[:6]:
            if explore <= 0:
                break
            t = h["tag"].lstrip("#")
            if t in exclude:
                continue
            more = query(t)
            if more:
                posts += more
                exclude.add(t)
                explore -= 1
        res = analyse(posts, tz, other_games=others) if posts else {}
        if res:
            res["searched"] = sorted(exclude)
            out["games"][key] = res
            all_posts += posts
            log(f"🔎 {prof.get('name', key)}: read {res['posts']} top Instagram posts → "
                f"tags {' '.join(h['tag'] for h in res['hashtags'][:5])}")

    # benchmark pages you chose (Business Discovery)
    bench = [b.lstrip("@") for b in (tcfg.get("benchmark_pages") or []) if b]
    bench_posts = []
    for u in bench[:10]:
        try:
            media = ig.business_discovery_media(u, limit=25)
            bench_posts += media
            out.setdefault("benchmarks", {})[u] = analyse(media, tz)
        except Exception as e:
            out["notes"].append(f"@{u}: {str(e)[:120]}")
    all_posts += bench_posts

    overall = analyse(all_posts, tz) if all_posts else {}
    want = int(cfg["schedule"]["max_per_day"])
    hrs = best_hours(overall.get("hour_weights") or [], want)
    if hrs and len(all_posts) >= 15:
        out["best_hours_local"] = hrs
        out["post_times"] = [f"{max(h - 1, 8):02d}:40" for h in hrs]   # be live just before the peak
    out["overall_caption_stats"] = overall.get("caption_stats", {})
    if not out["games"] and not bench_posts:
        prev = load(cfg)
        if prev.get("games"):          # keep the last good read rather than blanking it
            prev["notes"] = out["notes"]
            path(cfg).write_text(json.dumps(prev, indent=2, ensure_ascii=False), encoding="utf-8")
            return prev
    path(cfg).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    _report(cfg, out)
    if out.get("post_times"):
        log(f"🕒 top posts in your niche go live around {', '.join(f'{h}:00' for h in hrs)} → "
            f"posting at {', '.join(out['post_times'])} until your own follower data takes over")
    return out


def _report(cfg, d: dict) -> None:
    lines = [f"INSTAGRAM SCOUT · {d.get('updated', '')[:16]}", ""]
    for g, r in (d.get("games") or {}).items():
        cs = r.get("caption_stats", {})
        lines += [f"{g.upper()}  ({r.get('posts')} top posts read, searched {' '.join('#' + t for t in r.get('searched', []))})",
                  "  trending tags: " + " ".join(h["tag"] for h in r.get("hashtags", [])[:10]),
                  f"  captions: ~{cs.get('median_length')} chars, {int(cs.get('with_question', 0) * 100)}% ask a question, "
                  f"{int(cs.get('with_cta', 0) * 100)}% have a CTA, {int(cs.get('with_emoji', 0) * 100)}% use emoji, "
                  f"~{cs.get('median_hashtags')} hashtags"]
        lines += [f"  · {c['line']}  ({c.get('likes') or '?'} likes, {c.get('comments') or 0} comments)"
                  for c in r.get("top_captions", [])[:5]]
        lines.append("")
    if d.get("post_times"):
        lines.append(f"BEST TIMES (top posts' publish hours): {', '.join(d['post_times'])}")
    if d.get("notes"):
        lines += ["", "NOTES"] + [f"  {n}" for n in d["notes"]]
    out = cfg.output / "reports" / f"scout-{d.get('updated', '')[:10]}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("```\n" + "\n".join(lines) + "\n```\n", encoding="utf-8")


# ── used by the rest of the agent ───────────────────────────────────────────

def trending_tags(cfg, game: str, n: int = 8) -> list[str]:
    g = (load(cfg).get("games") or {}).get(game) or {}
    return [h["tag"] for h in g.get("hashtags", [])[:n]]


def enrich_profile(cfg, profile: dict) -> dict:
    """Profile with this week's trending Instagram tags merged into its hashtag bank (core tags stay first)."""
    tags = trending_tags(cfg, profile.get("key", ""))
    if not tags:
        return profile
    core = list(profile.get("hashtags") or [])[:2]
    seen, bank = set(), []
    for t in core + tags + list(profile.get("hashtags") or []):
        if t.lower() not in seen:
            seen.add(t.lower())
            bank.append(t)
    return {**profile, "hashtags": bank, "trending_tags": tags}


def prompt_notes(cfg, game: str) -> str:
    d = load(cfg)
    g = (d.get("games") or {}).get(game) or {}
    if not g:
        return ""
    cs = g.get("caption_stats", {})
    parts = [f"Read live from Instagram's top posts for {' '.join('#' + t for t in g.get('searched', []))} "
             f"({d.get('updated', '')[:10]}, {g.get('posts')} posts):",
             "  hashtags on top posts now: " + " ".join(h["tag"] for h in g.get("hashtags", [])[:10]),
             f"  their captions: ~{cs.get('median_length')} chars, {int(cs.get('with_question', 0) * 100)}% ask a "
             f"question, {int(cs.get('with_cta', 0) * 100)}% have a call to action, "
             f"{int(cs.get('with_emoji', 0) * 100)}% use emoji",
             "  opening lines of the best ones (learn the pattern, never copy):"]
    parts += [f"    - {c['line']}" for c in g.get("top_captions", [])[:5]]
    return "\n".join(parts)[:1400]


def evidence_text(cfg) -> str:
    d = load(cfg)
    if not d.get("games"):
        return ""
    out = ["\nLive Instagram data (top posts by hashtag):"]
    for g, r in d["games"].items():
        out.append(f"  {g}: tags {' '.join(h['tag'] for h in r.get('hashtags', [])[:8])}; "
                   f"best openers: " + " | ".join(c["line"][:70] for c in r.get("top_captions", [])[:3]))
    if d.get("post_times"):
        out.append(f"  top posts go live around: {', '.join(d['post_times'])}")
    return "\n".join(out)


def post_times(cfg) -> list[str]:
    d = load(cfg)
    try:
        fresh = datetime.now(timezone.utc) - datetime.fromisoformat(d["updated"]) < timedelta(days=14)
    except Exception:
        fresh = False
    return list(d.get("post_times") or []) if fresh else []
