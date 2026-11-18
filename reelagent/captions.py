"""Claude watches stills from each moment, rates it, and writes the post copy.

Per moment:
  1. Claude sees 4 stills (+ your commentary transcript) and returns a 1-10 quality score,
     spoiler/graphic flags, THREE on-screen hook options built on the ig-reel hook formulas,
     the caption and up to 5 hashtags.
  2. hookscore.py (ig-reel) scores the three hooks; the winner goes on the video.
  3. humanize.py (ig-human) strips the AI fingerprint from the caption, caption.py (ig-caption)
     lints it and detect.py scores it. If it fails, Claude gets one rewrite with the problems listed.
  4. What the audit learned about this page (output/state/learnings.json), today's trend brief (trends.py)
     and Instagram's stated ranking signals are fed into the prompt. Claude also picks the edit style.
Without an API key a template fallback is used.
"""
from __future__ import annotations

import base64
import json
import random
import re
import tempfile
from pathlib import Path

from . import media, toolkit

IG_MAX_HASHTAGS = 5          # Instagram hard cap since Dec 2025

SYSTEM = """You are the editor and social media manager of a fast-growing gaming clips page on \
Instagram Reels. You look at stills from a gameplay moment and write copy that earns watch-time honestly.

Rules:
- Describe only what is actually visible or said. Never invent kills, scores, records or events.
- On-screen hooks: 4-7 words, plain words, no emojis, no hashtags. Each of the three uses a DIFFERENT \
hook formula from the list provided, adapted to gaming. Specific and concrete beats generic \
("3 COPS, 1 BRIDGE, 0 BRAKES" beats "CRAZY MOMENT").
- Instagram caption: first line is the hook in words a stranger understands in under 125 characters \
and names the game (people search it), then at most one more short line, then ONE call to action. \
Prefer a SEND call to action ("send this to your duo who...") because DM shares drive reach to new people; \
otherwise a comment prompt ("rate it /10"). No hashtags in the caption text. No em dashes, no \
"game-changer", no "stop scrolling", no "follow for more".
- Hashtags: at most 5, 1-2 big game tags + 2-3 specific ones. Each starts with #.
- Flag cutscenes, story endings and big plot reveals: publishers take those down.
- Rate quality like an editor choosing what to post from a long session. High (8-10): kills and multi-kills, \
clutches, big explosions, crashes, stunts, goals, saves, skill shots, genuinely funny fails. Middle (5-7): \
decent action with no clear payoff. Low (1-4): the player just dying with nothing before it, kill-cams and \
respawn or "killed by" screens, walking, looting, reloading, aiming at nothing, menus, loading, pause or \
black screens, the camera pointing away from the TV. Be strict: posting a weak clip hurts the page more than \
posting nothing.
- Flag graphic violence (gore, dismemberment) honestly.
- music_mood: the background music that fits this clip: hype, intense, epic, chill, funny or suspense.
- edit_style: the kind of reel that suits THIS clip best. Options: classic (payoff-first teaser + punch zoom + \
big hook: any strong moment), slowmo (slow motion on the payoff: clean shots, goals, stunts), replay (full \
speed, then a slow-motion instant replay: clutches, goals), freeze (freeze-frame with a label just before it \
happens: fails, ambushes, "he has no idea" moments), meme_bar (white caption bar with a relatable line: \
funny or relatable moments), pov (a "POV: ..." caption box: first-person moments the viewer can put \
themselves in). Be creative: do not default to classic when another kind fits, and follow the trend brief.
- meme_line: ALWAYS write one, it is used for meme_bar and pov: a relatable one-liner in sentence case \
(max 12 words), e.g. "when you think you finally lost the cops" or "POV: you trusted the bridge".
- freeze_line: 2-4 words for the freeze-frame label, about what is about to happen, e.g. "he has no idea".
- end_card: a 2-4 word question for the last second that makes people comment ("Rate this /10", \
"Clutch or luck?"), or empty when the clip should just end.
- look: the colour grade a pro editor would give these stills: vice (neon, warm pink highlights, teal \
shadows; sunny or night city), tactical (crisp teal-orange; shooters), stadium (bright broadcast; football), \
cinematic (film look; story, scenery, drives), night (dark scenes that need lifting) or clean (already looks great).
- impact: true if the payoff is a hit, crash, explosion, kill, goal or big landing (adds shake + flash), \
false for calm or funny moments.
- Use the live Instagram data (top posts' hashtags and caption patterns) when it is provided: match what \
works there in your own words, never copy a caption.
Return ONLY a JSON object, no prose."""

SCHEMA = """{
  "quality": <1-10 how likely this clip is to hold a stranger's attention>,
  "what_happens": "<one sentence, what is actually happening>",
  "is_cutscene_or_spoiler": <true|false>,
  "graphic_violence": <true|false>,
  "hook_options": ["<hook 1>", "<hook 2>", "<hook 3>"],
  "music_mood": "<hype|intense|epic|chill|funny|suspense>",
  "edit_style": "<classic|slowmo|replay|freeze|meme_bar|pov>",
  "meme_line": "<relatable one-liner or POV line>",
  "freeze_line": "<2-4 words>",
  "end_card": "<2-4 word question, or empty>",
  "look": "<vice|tactical|stadium|cinematic|night|clean>",
  "impact": <true|false>,
  "instagram_caption": "<caption without hashtags>",
  "hashtags": ["#tag", "..."]
}"""


_VIEW: dict = {}          # path -> VideoInfo, so stills show the straightened screen (set by pipeline)


def _frames(video: Path, times: list[float], tmp: Path) -> list[dict]:
    blocks = []
    info = _VIEW.get(str(Path(video).resolve()))
    for i, t in enumerate(times):
        f = media.grab_frame(video, t, tmp / f"f{i}.jpg", width=640, pre=info.pre(t - 1, t + 1) if info else "")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(f.read_bytes()).decode()}})
    return blocks


def parse_json(text: str):
    m = re.search(r"[\{\[].*[\}\]]", text, re.S)
    if not m:
        raise ValueError(f"No JSON in model reply: {text[:200]}")
    return json.loads(m.group(0))


def _clean_hashtags(tags, bank: list[str]) -> list[str]:
    out = []
    for t in list(tags or []) + list(bank):
        t = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if len(t) > 1 and t.lower() not in {x.lower() for x in out}:
            out.append(t)
        if len(out) >= IG_MAX_HASHTAGS:
            break
    return out


def learnings_text(cfg) -> str:
    p = cfg.state_dir / "learnings.json"
    if not p.exists():
        return ""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return ""
    return d.get("prompt_notes", "")


def ask_claude(cfg, system: str, content: list, max_tokens: int = 1200) -> str:
    """Ask whichever AI provider is configured (Claude, free Gemini). Name kept for older modules."""
    from . import ai
    return ai.ask(cfg, system, content, max_tokens)


def _finish(copy: dict, profile: dict) -> dict:
    """Pick the best hook with hookscore.py, humanize + lint the caption."""
    opts = [o for o in copy.get("hook_options") or [] if o] or [copy.get("hook_text", "")]
    opts = [" ".join(str(o).split()[:8]) for o in opts]
    hook, score, verdict, table = toolkit.best_hook(opts)
    copy["hook_text"] = hook or random.choice(profile.get("hooks") or ["WAIT FOR IT"])
    copy["hook_score"], copy["hook_verdict"], copy["hook_table"] = score, verdict, table
    copy["hook_formula"] = toolkit.classify_hook(copy["hook_text"])[1]
    if copy.get("music_mood") not in ("hype", "intense", "epic", "chill", "funny", "suspense"):
        copy["music_mood"] = profile.get("music_mood", "hype")
    from .looks import LOOKS
    if copy.get("look") not in LOOKS:
        copy["look"] = None
    from .formats import FORMATS
    if copy.get("edit_style") not in FORMATS:
        copy["edit_style"] = None
    for k, n in (("meme_line", 14), ("freeze_line", 4), ("end_card", 5)):
        copy[k] = " ".join(str(copy.get(k) or "").split()[:n])

    cap, report = toolkit.humanize(str(copy.get("instagram_caption", "")))
    copy["instagram_caption"] = cap
    copy["hashtags"] = _clean_hashtags(copy.get("hashtags"), profile.get("hashtags", []))
    full = instagram_text(copy)
    lint = toolkit.lint_caption(full, keywords=[profile.get("name", "")])
    copy["caption_lint"] = {"verdict": lint["verdict"], "problems": toolkit.lint_problems(lint)}
    copy["human_score"] = toolkit.human_score(cap)
    copy["humanizer_changes"] = sum(len(v) for k, v in report.items() if k != "structures")
    return copy


# Without an AI key the agent can't see what happens in a clip, so these stay general. They are rotated
# so no two recent reels share a hook or a caption, and matched to the kind of reel once it is chosen.
TEMPLATE_HOOKS = {
    "any": ["WAIT FOR IT", "DID NOT EXPECT THAT", "WATCH TILL THE END", "THIS GOT OUT OF HAND", "NO WAY THIS WORKED",
            "I WAS NOT READY", "HOW DID THAT HAPPEN", "THE ENDING THOUGH", "ZERO HESITATION", "THIS LOBBY WAS WILD"],
    "replay": ["WATCH THE REPLAY", "RUN IT BACK", "ONE MORE TIME, SLOWER", "SLOW IT DOWN"],
    "slowmo": ["IN SLOW MOTION", "WATCH IT SLOW", "FRAME BY FRAME", "SO CLEAN"],
    "freeze": ["HE HAS NO IDEA", "WATCH WHAT HAPPENS NEXT", "PAUSE. NOW WATCH", "THIS IS WHERE IT TURNS"],
    "top3": ["TOP 3 FROM ONE SESSION", "RANKING TODAY'S BEST", "3 CLIPS, 1 WINNER"],
    "montage": ["BEST BITS FROM ONE GAME", "ONE SESSION, ALL THE CHAOS", "TODAY'S HIGHLIGHTS"],
}
TEMPLATE_CAPTIONS = [
    "{name}, no edits needed. Rate it out of 10.",
    "Would you have done the same? {name} clip #{n}.",
    "Send this to the friend you play {name} with.",
    "{name} clip #{n}. W or L?",
    "Still thinking about this one. {name} clip #{n}.",
    "Tell me you've had a moment like this in {name}.",
]
TEMPLATE_CAPTIONS_ANY_GAME = [    # when the game isn't known, don't print "Gaming" where a game's name belongs
    "No edits needed. Rate it out of 10.",
    "Would you have done the same? Clip #{n}.",
    "Send this to the friend you squad up with.",
    "Clip #{n}. W or L?",
    "Still thinking about this one.",
    "Tell me you've had a moment like this.",
]
TEMPLATE_LINES = {   # meme_bar / pov lines and freeze labels, when there is no AI to write them
    "pov": ["POV: you thought this would be easy", "POV: one more game before bed", "POV: the lobby didn't see it coming"],
    "freeze": ["He has no idea", "Watch this", "Not yet..."],
}


def template_hook(profile: dict, style: str, avoid: list[str], seed: int) -> str:
    bank = TEMPLATE_HOOKS.get(style, []) + list(profile.get("hooks") or []) + TEMPLATE_HOOKS["any"]
    seen = {h.upper() for h in avoid}
    fresh = [h for h in dict.fromkeys(bank) if h.upper() not in seen] or list(dict.fromkeys(bank))
    first = TEMPLATE_HOOKS.get(style, [])
    pool = [h for h in fresh if h in first] or fresh
    return pool[seed % len(pool)]


def fallback(profile: dict, series_no: int, avoid: list[str] | None = None) -> dict:
    name = profile.get("name", "Gaming")
    hook = template_hook(profile, "any", avoid or [], series_no)
    bank = TEMPLATE_CAPTIONS if profile.get("key") not in (None, "", "default") else TEMPLATE_CAPTIONS_ANY_GAME
    cap = bank[series_no % len(bank)].format(name=name, n=series_no)
    copy = {
        "quality": 7, "what_happens": "", "is_cutscene_or_spoiler": False, "graphic_violence": False,
        "hook_options": [hook],
        "music_mood": profile.get("music_mood", "hype"),
        "meme_line": TEMPLATE_LINES["pov"][series_no % len(TEMPLATE_LINES["pov"])],
        "freeze_line": TEMPLATE_LINES["freeze"][series_no % len(TEMPLATE_LINES["freeze"])],
        "instagram_caption": cap,
        "hashtags": list(profile.get("hashtags", []))[:2] + list(profile.get("trending_tags", []))[:3]
                    or profile.get("hashtags", []),
        "source": "template",
    }
    return _finish(copy, profile)


def write_copy(cfg, video: Path, moment, profile: dict, transcript: str, series_no: int, brief: str = "",
               avoid: list[str] | None = None) -> dict:
    """Return the copy dict for one moment (AI if a key is set, else template).
    `brief` adds an instruction for special reels (e.g. a top-3 countdown built from several moments).
    `avoid`: hooks used on recent reels."""
    if not cfg.ai_available:
        return fallback(profile, series_no, avoid)
    from . import trends   # late import: trends uses parse_json from this module

    trend = trends.prompt_notes(cfg, profile.get("key", ""))
    system = SYSTEM + "\n\n" + trends.ALGORITHM

    span = moment.end - moment.start
    times = [moment.start + span * 0.15, moment.peak - 1.0, moment.peak, min(moment.end - 0.5, moment.peak + 1.5)]
    notes = learnings_text(cfg)
    from .revise import owner_notes
    owner = "\n".join(f"- {x}" for x in owner_notes(cfg))
    prompt = (
        f"Game: {profile.get('name')}\n"
        f"This is clip #{series_no} in the page's {profile.get('name')} series.\n"
        f"The 4 stills are from a {span:.0f}s clip: start, 1s before the payoff, the payoff, just after.\n"
        f"Player commentary transcript (may be empty): {transcript or '(none)'}\n"
        f"Hashtags the page usually uses (pick from these or better ones): {' '.join(profile.get('hashtags', []))}\n"
        f"Page call to action: {profile.get('cta', '')}\n"
        + (f"\nWhat has worked on THIS page so far (from its own stats, trust this over defaults):\n{notes}\n" if notes else "")
        + (f"\nThe page owner's standing wishes (always follow these):\n{owner}\n" if owner else "")
        + (f"\nWhat's trending this week (live research):\n{trend}\n" if trend else "")
        + (f"\nSPECIAL: {brief}\n" if brief else "")
        + f"\nHook formulas (use three different ones, adapted to gaming):\n{toolkit.formula_menu()}\n\n"
        f"Reply with JSON exactly in this shape:\n{SCHEMA}"
    )
    try:
        with tempfile.TemporaryDirectory() as td:
            content = _frames(video, times, Path(td)) if cfg["ai"].get("look_at_frames", True) else []
            content.append({"type": "text", "text": prompt})
            data = parse_json(ask_claude(cfg, system, content))
        data["quality"] = int(data.get("quality", 5))
        from . import ai
        data["source"] = ai.provider(cfg)
        data = _finish(data, profile)

        # one rewrite if the caption linter or the humanizer panel is unhappy
        if data["caption_lint"]["verdict"] == "FIX" or data["human_score"][1] == "FLAGGED":
            fix = (f"Rewrite this Instagram caption to fix these problems: "
                   f"{'; '.join(data['caption_lint']['problems']) or 'reads machine-written'}.\n"
                   f"Caption: {data['instagram_caption']}\n"
                   'Return JSON: {"instagram_caption": "..."}')
            data["instagram_caption"] = parse_json(
                ask_claude(cfg, system, [{"type": "text", "text": fix}], 400))["instagram_caption"]
            data = _finish(data, profile)
        return data
    except Exception as e:  # network/API trouble shouldn't stop the pipeline
        print(f"   ! AI copy failed ({e.__class__.__name__}: {e}); using template")
        return fallback(profile, series_no)


def detect_game(cfg, video: Path, info) -> str | None:
    """Which configured game is this? The AI looks at 3 stills. None if unsure or no AI."""
    keys = {k: v.get("name", k) for k, v in cfg["games"].items() if k != "default"}
    if not keys:
        return None
    times = [info.duration * f for f in (0.2, 0.5, 0.8)]
    prompt = ("Which video game is this gameplay footage from? Options:\n"
              + "\n".join(f"- {k}: {n}" for k, n in keys.items())
              + "\n- other: a different game, or not gameplay\n"
              'Reply ONLY with JSON: {"game": "<option key>", "confidence": <0-1>, "seen": "<what told you>"}')
    try:
        with tempfile.TemporaryDirectory() as td:
            content = _frames(video, times, Path(td))
            content.append({"type": "text", "text": prompt})
            d = parse_json(ask_claude(cfg, "You identify video games from screenshots. Be honest when unsure.",
                                      content, 200))
        g = str(d.get("game", "")).lower()
        if g in keys and float(d.get("confidence", 0)) >= 0.6:
            return g
    except Exception as e:
        print(f"   ! game check failed ({e.__class__.__name__}: {e})")
    return None


def instagram_text(copy: dict) -> str:
    cap = copy.get("instagram_caption", "").strip()
    tags = " ".join(copy.get("hashtags", [])[:IG_MAX_HASHTAGS])
    return f"{cap}\n\n{tags}".strip()[:2200]
