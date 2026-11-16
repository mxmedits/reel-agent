"""Finds the most exciting moments in a long gameplay recording.

No ML needed: gunfire bursts, explosions, crowd roars after a goal and hype
commentary all show up as loudness spikes, and action shows up as motion.
Static screens (menus, loading, pause) are pushed down.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

from . import media

WIN = 0.5  # seconds per analysis step


@dataclass
class Moment:
    peak: float    # seconds into the source where the payoff happens
    start: float
    end: float
    score: float

    def to_dict(self):
        return asdict(self)


def _robust_z(x: np.ndarray) -> np.ndarray:
    if len(x) == 0:
        return x
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826 + 1e-6
    return (x - med) / mad


def _rolling_median(x: np.ndarray, width: int) -> np.ndarray:
    width = max(3, width | 1)
    if len(x) < width:
        return np.full_like(x, np.median(x))
    pad = width // 2
    xp = np.pad(x, pad, mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(xp, width)
    return np.median(view, axis=1)


def _smooth(x: np.ndarray, width: int) -> np.ndarray:
    width = max(1, width)
    if width == 1 or len(x) == 0:
        return x
    k = np.ones(width) / width
    return np.convolve(np.pad(x, width, mode="edge"), k, mode="same")[width:-width]


def _fit(a: np.ndarray, n: int) -> np.ndarray:
    if len(a) == n:
        return a
    if len(a) == 0:
        return np.zeros(n, dtype=np.float32)
    if len(a) > n:
        return a[:n]
    return np.pad(a, (0, n - len(a)), mode="edge")


def excitement_curve(video: Path, info: media.VideoInfo) -> np.ndarray:
    db = media.audio_rms(video, WIN) if info.has_audio else np.zeros(0, dtype=np.float32)
    motion, detail = media.motion_profile(video, fps=1 / WIN)
    n = max(len(db), len(motion), 1)
    db, motion, detail = _fit(db, n), _fit(motion, n), _fit(detail, n)

    if np.any(db):
        mad = np.median(np.abs(db - np.median(db))) * 1.4826 + 1e-6
        local = (db - _rolling_median(db, int(30 / WIN))) / mad   # louder than the last ~30 s
        audio = 0.6 * local + 0.4 * _robust_z(db)
    else:
        audio = np.zeros(n)
    move = _robust_z(motion)

    score = 0.65 * np.clip(audio, -3, 6) + 0.35 * np.clip(move, -3, 6)
    # menus / loading / pause screens: almost no motion or almost flat image
    static = (_smooth(motion, int(3 / WIN)) < 0.6) | (detail < 8)
    score = score - 3.0 * static
    # a burst that is sustained (crowd roar, multi-kill) beats a single click
    return 0.7 * _smooth(score, int(2 / WIN)) + 0.3 * _smooth(score, int(6 / WIN))


def find_moments(video: Path, info: media.VideoInfo, length: float, count: int,
                 min_gap: float) -> list[Moment]:
    dur = info.duration
    if dur <= length + 1:
        return [Moment(peak=dur * 0.6, start=0.0, end=dur, score=0.0)]

    curve = excitement_curve(video, info)
    order = np.argsort(curve)[::-1]
    chosen: list[Moment] = []
    for idx in order:
        t = idx * WIN + WIN / 2
        if t < 3 or t > dur - 1.5:
            continue
        if any(abs(t - m.peak) < min_gap for m in chosen):
            continue
        # refine the payoff to the loudest instant within ±1 s
        lo, hi = max(0, idx - 2), min(len(curve), idx + 3)
        peak = float((lo + int(np.argmax(curve[lo:hi]))) * WIN + WIN / 2)
        start = peak - length * 0.62          # build-up first, payoff ~60 % in
        start = float(min(max(0.0, start), dur - length))
        chosen.append(Moment(peak=round(peak, 2), start=round(start, 2),
                             end=round(start + length, 2), score=round(float(curve[idx]), 3)))
        if len(chosen) >= count:
            break
    return chosen
