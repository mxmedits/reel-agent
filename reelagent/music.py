"""Mood-matched music with the drop lined up on the payoff.

Library layout (put tracks you have the rights to use here: royalty-free / licensed):
    music/hype/  music/intense/  music/epic/  music/chill/  music/funny/  music/suspense/
    music/gta6/hype/ ...       (optional per-game folders win over the shared ones)
Claude picks the mood for each clip from what it sees; the game profile's music_mood is the fallback.
"""
from __future__ import annotations

import random
from pathlib import Path

import numpy as np

from . import media

MOODS = ["hype", "intense", "epic", "chill", "funny", "suspense"]
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
# if the exact mood folder is empty, a close mood stands in (never a clashing one)
NEIGHBORS = {"hype": ["intense", "epic"], "intense": ["hype", "epic"], "epic": ["intense", "hype"],
             "suspense": ["intense", "epic"], "chill": ["funny"], "funny": ["chill", "hype"]}


def library(cfg) -> Path:
    return cfg._path((cfg.get("music", {}) or {}).get("library", "./music"))


def ensure_library(cfg) -> None:
    root = library(cfg)
    for mood in MOODS:
        (root / mood).mkdir(parents=True, exist_ok=True)
    readme = root / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Drop royalty-free or licensed tracks into the mood folders.\n"
            "Optional per-game folders win: music/gta6/hype/, music/cod/intense/, music/fc27/hype/ ...\n"
            "Empty library = the agent uses an AI voiceover instead (if enabled).\n"
            "Only use music you have the rights to: Instagram mutes or removes reels with unlicensed songs.\n",
            encoding="utf-8")


def _tracks(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXT and p.is_file()]


def pick_track(cfg, game: str, mood: str | None, recent: list[str] | None = None) -> Path | None:
    root = library(cfg)
    mood = mood if mood in MOODS else None
    order = []
    if mood:
        order += [root / game / mood, root / mood]
        for near in NEIGHBORS.get(mood, []):
            order += [root / game / near, root / near]
    order += [root / game]
    for folder in order:
        tracks = _tracks(folder)
        fresh = [t for t in tracks if str(t) not in (recent or [])] or tracks   # avoid repeating the last few
        if fresh:
            return random.choice(fresh)
    return None


def drop_time(track: Path, search: float = 120.0) -> float:
    """Seconds into the track where energy jumps the most (the drop). 0 if it can't tell."""
    try:
        db = media.audio_rms(track, window=0.25)[: int(search / 0.25)]
    except Exception:
        return 0.0
    if len(db) < 16:
        return 0.0
    before = np.convolve(db, np.ones(8) / 8, mode="same")
    jump = np.zeros_like(db)
    jump[8:-8] = before[16:] - before[:-16]          # loudness 2 s after vs 2 s before
    i = int(np.argmax(jump))
    return float(i * 0.25) if jump[i] > 4 else 0.0     # needs a >4 dB rise to count as a drop


def alignment(track: Path, payoff_in_video: float) -> tuple[float, float]:
    """(seek into track, delay before track starts) so the drop lands on the payoff."""
    d = drop_time(track)
    if d <= 0:
        return 0.0, 0.0
    if d >= payoff_in_video:
        return round(d - payoff_in_video, 2), 0.0
    return 0.0, round(payoff_in_video - d, 2)
