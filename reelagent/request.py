"""What the owner asked for when adding a clip, from the app's "What should the agent make?" sheet.

  kind    auto       the agent decides (short reels, plus a montage when there's plenty)
          shorts     each strong moment as its own reel (count: 1-5)
          highlight  the critical parts of the whole clip cut into one video (length: 30 s to 3 min)
          full       the whole clip as one video, lightly edited (no cuts); longer than `length`: the best
                     stretch of that length, or all of it with length "all" (Instagram's API takes 15 min)
          top3       a countdown of the 3 best moments
          montage    fast cuts of the best moments
  sound   auto | game (game sound only) | music (music only) | both
  voice   auto | off | on        AI voiceover
  text    auto | title (title only, no end question) | none (no text at all)
  note    anything else, in the owner's words: read for edit changes ("mute the game", "brighter"),
          times to include ("the crash at 1:20"), and handed to the caption writer
"""
from __future__ import annotations

import re

KINDS = {"auto", "shorts", "highlight", "full", "top3", "montage"}
SOUNDS, VOICES, TEXTS = {"auto", "game", "music", "both"}, {"auto", "off", "on"}, {"auto", "title", "none"}
API_MAX = 15 * 60          # Instagram's API takes reels up to 15 minutes
NAMES = {"auto": "the agent's choice", "shorts": "short reels", "highlight": "one highlight video",
         "full": "the full video", "top3": "a top 3 countdown", "montage": "a montage"}


def clean(raw) -> dict:
    p = raw if isinstance(raw, dict) else {}
    kind = str(p.get("kind") or "auto").lower()
    kind = kind if kind in KINDS else "auto"
    out = {"kind": kind,
           "sound": str(p.get("sound") or "auto").lower(), "voice": str(p.get("voice") or "auto").lower(),
           "text": str(p.get("text") or "auto").lower(), "note": " ".join(str(p.get("note") or "").split())[:600]}
    out["sound"] = out["sound"] if out["sound"] in SOUNDS else "auto"
    out["voice"] = out["voice"] if out["voice"] in VOICES else "auto"
    out["text"] = out["text"] if out["text"] in TEXTS else "auto"
    try:
        out["count"] = max(1, min(5, int(p.get("count") or 3)))
    except (TypeError, ValueError):
        out["count"] = 3
    length = p.get("length")
    if kind == "full" and str(length).lower() == "all":
        out["length"] = "all"
    else:
        try:
            out["length"] = max(15, min(API_MAX, int(float(length))))
        except (TypeError, ValueError):
            out["length"] = 180 if kind == "full" else 60
    return out


def is_default(p: dict) -> bool:
    return p["kind"] == "auto" and p["sound"] == p["voice"] == p["text"] == "auto" and not p["note"]


def fmt_len(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec} s" if sec < 60 else f"{sec // 60}:{sec % 60:02d} min" if sec % 60 else f"{sec // 60} min"


def label(p: dict) -> str:
    """Short text for the app: 'Highlight video, 1 min, music only'."""
    if not p or p.get("kind") == "auto" and is_default(p):
        return ""
    k = p["kind"]
    if k == "shorts":
        first = f"{p['count']} short reel{'s' if p['count'] != 1 else ''}"
    elif k == "highlight":
        first = f"Highlight video, {fmt_len(p['length'])}"
    elif k == "full":
        first = "Full video" + (", all of it" if p["length"] == "all" else f", up to {fmt_len(p['length'])}")
    else:
        first = {"auto": "Agent's choice", "top3": "Top 3 countdown", "montage": "Montage"}[k]
    bits = [first]
    bits += [{"game": "game sound only", "music": "music only", "both": "game sound and music"}.get(p["sound"], "")]
    bits += [{"off": "no voiceover", "on": "voiceover"}.get(p["voice"], "")]
    bits += [{"title": "title only", "none": "no title"}.get(p["text"], "")]
    return ", ".join(b for b in bits if b)


def times(note: str) -> list[float]:
    """Moments the owner named: '1:20', 'at 2:05', '0:45 and 3:10', '90s', '90 sec'."""
    out = []
    for m in re.finditer(r"\b(\d{1,2}):([0-5]\d)\b", note or ""):
        out.append(int(m.group(1)) * 60 + int(m.group(2)))
    for m in re.finditer(r"\b(\d{1,4})\s*(?:s|sec|secs|seconds)\b", note or "", re.I):
        out.append(int(m.group(1)))
    return sorted(set(out))[:6]


def overrides(p: dict) -> dict:
    """The per-reel overrides (the same ones the chat sets) for the owner's sound, voice and text choices,
    plus whatever the note asks for. Re-edits from the chat start from these."""
    o: dict = {}
    if p["sound"] == "game":
        o["music"] = False
    elif p["sound"] == "music":
        o["music"], o["game_audio"] = True, "off"
    elif p["sound"] == "both":
        o["music"] = True
    if p["voice"] == "off":
        o["voiceover"] = False
    elif p["voice"] == "on":
        o["voiceover"] = True
    if p["text"] == "title":
        o["end_card"] = ""
    elif p["text"] == "none":
        o["no_text"], o["end_card"] = True, ""
    if p["note"]:
        from .revise import plan_from_keywords
        kw = plan_from_keywords(p["note"], None)
        for k, v in kw.items():
            if k in ("reply", "cannot", "remember", "note_for_future", "different_moment", "shift", "hook",
                     "caption", "hashtags", "length", "style"):
                continue                     # moment, length and kind come from the sheet itself
            o.setdefault(k, v)
    return o


def brief(p: dict, extra: str = "") -> str:
    """A line for the caption writer about what the owner asked for."""
    parts = []
    if p["kind"] in ("highlight", "full"):
        parts.append(f"This post is {NAMES[p['kind']]} of the session ({extra}), not a single moment. Write the hooks "
                     "and the caption for the video as a whole.")
    if p["note"]:
        parts.append(f"The owner said about this clip: \"{p['note']}\". Follow it where it is about the caption or hook.")
    return " ".join(parts)
