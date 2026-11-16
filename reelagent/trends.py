"""Trend intelligence: what's working on Reels *this week*, refreshed daily.

Once a day the AI researches what's trending, from free sources first:
  - YouTube Data API (free key): this week's most-viewed Shorts per game + the Gaming chart
  - Instagram's trending sounds list (Audio API, free once Instagram is connected)
  - live web search when the provider offers it (Claude web search, or Gemini's Google Search grounding)
and writes a brief covering:
  - gaming Reel formats and edit styles that are trending right now
  - the music styles / sounds people use in gaming edits (search terms for Instagram's Audio API)
  - timely moments per game (launches, seasons, patches, Team of the Week, viral glitches)
  - hashtags people actually use now, and things to avoid (leaks, spoilers)
The brief is saved to output/state/trend_brief.json (+ a readable report) and fed into:
  hook/caption writing, the edit style picked for each clip, and the trending-sound search.

ALGORITHM is what Instagram has said publicly about ranking; it goes into every caption/hook prompt.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import ai
from .captions import parse_json

STYLES = ["classic", "slowmo", "meme_bar"]

ALGORITHM = """How Instagram ranks Reels (stated by Instagram's head, Adam Mosseri, 2025-2026):
- Top signals: watch time (completion, replays), sends per reach (DM shares: the strongest signal for reaching
  NON-followers), likes per reach (matters more with followers). Saves also count. Original content is favored.
How to apply it to every reel:
- Watch time: the first frame already shows action or the hook; the payoff lands before ~60% of the clip;
  end on a beat that makes the replay feel natural (loop).
- Sends: give a reason to DM it to a friend ("send this to your duo who always..."). Prefer a send/share
  call to action over "follow".
- Likes/comments: a clear payoff; "rate this /10" or a debate question.
- Search: name the game and the moment in plain words in the first caption line (Instagram search reads captions).
- At most 5 hashtags. No watermarks from other apps."""

SCHEMA = """{
  "summary": "<2 sentences: what's working for gaming Reels right now>",
  "formats": [{"name": "...", "how": "<how to apply it to a gameplay clip>"}],
  "edit_styles": {"recommended": "<one of: classic | slowmo | meme_bar>", "notes": "<when each works now>"},
  "music": {"genres": ["..."], "search_terms": ["<3-6 short terms to search Instagram's music library>"],
            "mood_bias": "<hype|intense|epic|chill|funny|suspense>"},
  "games": {"<game key>": {"angles": ["..."], "timely": ["<dated events/moments>"], "hashtags": ["#..."],
                            "avoid": ["..."]}},
  "caption_tips": ["..."],
  "algorithm_updates": ["<Instagram ranking/Reels changes announced in the last 30 days and how to adapt; [] if none>"],
  "sources": ["<url>", "..."]
}"""

SYSTEM = """You are a short-form video strategist for a gaming clips page on Instagram Reels.
Research the live web for what is trending THIS WEEK. Prefer recent sources (last 30 days): Instagram/Meta
announcements (Adam Mosseri, @creators, about.instagram.com), creator-economy news, gaming news, social media
marketing sites. Always check for recent changes to how Instagram ranks or recommends Reels. Be concrete and current;
say so when evidence is thin instead of guessing. Never recommend leaked/pre-release footage, story spoilers,
engagement bait that breaks Instagram rules, or reposting other creators' clips.
Edit styles the editor can do: classic (payoff-first teaser, punch zoom, big hook text),
slowmo (slow-motion on the payoff), meme_bar (white caption bar on top with a relatable line, video below).
Finish with ONLY the JSON object, no prose after it."""


def brief_path(cfg) -> Path:
    return cfg.state_dir / "trend_brief.json"


def load(cfg) -> dict:
    p = brief_path(cfg)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def youtube_evidence(cfg, log=print) -> str:
    """This week's top gaming Shorts (titles + views) from the free YouTube Data API. '' if no key."""
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        return ""
    base = "https://www.googleapis.com/youtube/v3"
    week = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = []
    try:
        for g, prof in cfg["games"].items():
            if g == "default":
                continue
            r = requests.get(f"{base}/search", timeout=30, params={
                "part": "snippet", "q": f"{prof.get('name', g)} #shorts", "type": "video", "order": "viewCount",
                "publishedAfter": week, "videoDuration": "short", "maxResults": 12, "key": key})
            r.raise_for_status()
            items = r.json().get("items", [])
            ids = ",".join(i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId"))
            views = {}
            if ids:
                v = requests.get(f"{base}/videos", timeout=30, params={"part": "statistics", "id": ids, "key": key})
                views = {x["id"]: int(x.get("statistics", {}).get("viewCount", 0)) for x in v.json().get("items", [])}
            lines.append(f"\nTop {prof.get('name', g)} Shorts this week:")
            for i in items:
                vid = i.get("id", {}).get("videoId")
                lines.append(f"  - {i['snippet']['title']} ({views.get(vid, 0):,} views)")
        r = requests.get(f"{base}/videos", timeout=30, params={
            "part": "snippet", "chart": "mostPopular", "videoCategoryId": "20", "regionCode": "US",
            "maxResults": 15, "key": key})
        if r.ok:
            lines.append("\nYouTube Gaming chart right now:")
            lines += [f"  - {i['snippet']['title']}" for i in r.json().get("items", [])]
    except Exception as e:
        log(f"  (YouTube trend data unavailable: {e})")
    return "\n".join(lines)[:6000]


def instagram_sound_evidence(cfg) -> str:
    p = cfg.state_dir / "ig_trending_audio.json"
    try:
        items = json.loads(p.read_text(encoding="utf-8"))["items"]
        return "\nTrending Instagram sounds (Audio API):\n" + "\n".join(
            f"  - {i.get('title')} - {i.get('display_artist', '')}" for i in items[:15])
    except Exception:
        return ""


def refresh(cfg, log=print) -> dict:
    """Research today's trends and save the brief. Returns it ({} if no AI is configured)."""
    if not ai.available(cfg):
        return {}
    games = {k: v.get("name", k) for k, v in cfg["games"].items() if k != "default"}
    today = datetime.now().strftime("%Y-%m-%d")
    from . import scout
    evidence = youtube_evidence(cfg, log) + instagram_sound_evidence(cfg) + scout.evidence_text(cfg)
    prompt = (f"Today is {today}. The page posts gameplay clips of: "
              + ", ".join(f"{k} = {n}" for k, n in games.items())
              + (f"\n\nFresh evidence collected today (real data, weigh it heavily):{evidence}" if evidence else "")
              + "\n\nUse web search if you have it. Return this week's trend brief as JSON in exactly this shape:\n"
              + SCHEMA)
    try:
        text = ai.ask(cfg, SYSTEM, [{"type": "text", "text": prompt}], max_tokens=4000, search=True)
    except Exception as e:
        if not evidence:
            log(f"! trend research unavailable ({e})")
            return load(cfg)
        try:   # no web search on this provider/tier: build the brief from the free evidence alone
            text = ai.ask(cfg, SYSTEM, [{"type": "text", "text": prompt}], max_tokens=4000)
        except Exception as e2:
            log(f"! trend research unavailable ({e2})")
            return load(cfg)
    try:
        brief = parse_json(text[text.find("{"):] if "{" in text else text)
    except Exception as e:
        log(f"! trend brief unreadable ({e}); keeping the previous one")
        return load(cfg)
    if brief.get("edit_styles", {}).get("recommended") not in STYLES:
        brief.setdefault("edit_styles", {})["recommended"] = "classic"
    brief["date"] = today
    brief["provider"] = ai.provider(cfg)
    brief_path(cfg).write_text(json.dumps(brief, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_report(cfg, brief)
    log(f"📈 trend brief updated ({brief['provider']}): {brief.get('summary', '')[:120]}")
    return brief


def _write_report(cfg, b: dict) -> None:
    lines = [f"TRENDS · {b.get('date')}", "", b.get("summary", ""), "", "FORMATS"]
    lines += [f"  - {f.get('name')}: {f.get('how')}" for f in b.get("formats", [])]
    es = b.get("edit_styles", {})
    lines += ["", f"EDIT STYLE: {es.get('recommended')}  ({es.get('notes', '')})"]
    m = b.get("music", {})
    lines += ["", f"MUSIC: {', '.join(m.get('genres', []))} · searching: {', '.join(m.get('search_terms', []))}"]
    for g, d in (b.get("games") or {}).items():
        lines += ["", g.upper()]
        lines += [f"  angle: {a}" for a in d.get("angles", [])]
        lines += [f"  timely: {t}" for t in d.get("timely", [])]
        if d.get("hashtags"):
            lines.append("  tags: " + " ".join(d["hashtags"][:8]))
        lines += [f"  avoid: {a}" for a in d.get("avoid", [])]
    if b.get("caption_tips"):
        lines += ["", "CAPTIONS"] + [f"  - {t}" for t in b["caption_tips"]]
    if b.get("algorithm_updates"):
        lines += ["", "ALGORITHM CHANGES"] + [f"  - {t}" for t in b["algorithm_updates"]]
    if b.get("sources"):
        lines += ["", "SOURCES"] + [f"  {s}" for s in b["sources"][:12]]
    out = cfg.output / "reports" / f"trends-{b.get('date')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("```\n" + "\n".join(lines) + "\n```\n", encoding="utf-8")


def prompt_notes(cfg, game: str) -> str:
    """Compact trend context for one clip's prompt (empty if no brief yet)."""
    b = load(cfg)
    if not b:
        from . import scout
        return scout.prompt_notes(cfg, game)
    g = (b.get("games") or {}).get(game, {})
    parts = [f"Trend brief ({b.get('date')}): {b.get('summary', '')}"]
    if b.get("formats"):
        parts.append("Formats working now: " + "; ".join(f"{f.get('name')} ({f.get('how')})" for f in b["formats"][:4]))
    if g.get("angles"):
        parts.append("Angles for this game: " + "; ".join(g["angles"][:4]))
    if g.get("timely"):
        parts.append("Timely: " + "; ".join(g["timely"][:3]))
    if g.get("hashtags"):
        parts.append("Hashtags in use now: " + " ".join(g["hashtags"][:8]))
    if g.get("avoid"):
        parts.append("Avoid: " + "; ".join(g["avoid"][:3]))
    es = b.get("edit_styles", {})
    if es.get("recommended"):
        parts.append(f"Edit style trending: {es['recommended']} ({es.get('notes', '')})")
    if b.get("caption_tips"):
        parts.append("Caption tips: " + "; ".join(b["caption_tips"][:3]))
    if b.get("algorithm_updates"):
        parts.append("Recent Instagram ranking changes: " + "; ".join(map(str, b["algorithm_updates"][:3])))
    from . import scout
    live = scout.prompt_notes(cfg, game)
    return ("\n".join(parts)[:1800] + ("\n" + live if live else "")).strip()


def music_terms(cfg) -> list[str]:
    return [t for t in (load(cfg).get("music") or {}).get("search_terms", []) if isinstance(t, str)][:6]
