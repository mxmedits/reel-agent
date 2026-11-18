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


def _signals(video: Path, info: media.VideoInfo, n_hint: int) -> dict:
    """Per 0.5 s: motion, detail, brightness, colour and red-ness of the GAME picture only.
    Filmed off a TV: measured on the straightened screen, so the phone moving doesn't count as action."""
    tr = info.screen
    if tr is not None and tr.rect is not None and len(tr.rect):
        f = tr.rect.astype(np.float32)
        shake = tr.shake if tr.shake is not None else np.zeros(len(f))
    else:
        f = media.rgb_frames(video, 1 / WIN, 64, 36, pre=info.pre()).astype(np.float32)
        shake = np.zeros(len(f))
    if len(f) == 0:
        z = np.zeros(n_hint)
        return {"motion": z, "detail": z, "luma": z, "sat": z, "red": z, "shake": z}
    y = f[..., 0] * 0.299 + f[..., 1] * 0.587 + f[..., 2] * 0.114
    diff = np.abs(np.diff(y, axis=0)).mean(axis=(1, 2)) if len(y) > 1 else np.zeros(1)
    mx, mn = f.max(axis=3), f.min(axis=3)
    return {
        "motion": np.concatenate([[diff[0]], diff]),
        "detail": y.std(axis=(1, 2)),
        "luma": y.mean(axis=(1, 2)),
        "sat": (mx - mn).mean(axis=(1, 2)),
        # a red wash over the picture: taking damage, dying (shooters), wanted / wasted screens
        "red": np.clip(f[..., 0] - (f[..., 1] + f[..., 2]) / 2, 0, None).mean(axis=(1, 2)),
        "shake": shake,
    }


def excitement_curve(video: Path, info: media.VideoInfo, detail: bool = False):
    """Score per 0.5 s. With detail=True also returns the 'avoid' curve (0-1: don't put this in a reel)."""
    db = media.audio_rms(video, WIN) if info.has_audio else np.zeros(0, dtype=np.float32)
    sig = _signals(video, info, len(db))
    n = max(len(db), len(sig["motion"]), 1)
    db = _fit(db, n)
    sig = {k: _fit(np.asarray(v, dtype=np.float32), n) for k, v in sig.items()}
    filmed = info.screen is not None

    if np.any(db):
        mad = np.median(np.abs(db - np.median(db))) * 1.4826 + 1e-6
        local = (db - _rolling_median(db, int(30 / WIN))) / mad   # louder than the last ~30 s
        audio = 0.6 * local + 0.4 * _robust_z(db)
    else:
        audio = np.zeros(n)
    move = _robust_z(sig["motion"])
    # flashes: muzzle fire, explosions, hit markers (a jump in brightness against the last few seconds)
    flash = np.clip((sig["luma"] - _rolling_median(sig["luma"], int(4 / WIN))) / 12.0, 0, 3)

    # a phone records the room as well as the TV (voices, the controller), so trust the picture more
    wa, wm = (0.4, 0.45) if filmed else (0.6, 0.3)
    score = wa * np.clip(audio, -3, 6) + wm * np.clip(move, -3, 6) + 0.15 * flash

    # the low points nobody wants to watch
    static = ((_smooth(sig["motion"], int(3 / WIN)) < 0.6) | (sig["detail"] < 8)).astype(np.float32)   # menus, pauses
    dark = (sig["luma"] < 28).astype(np.float32)                                                    # loading, black
    sat_z = (sig["sat"] - _rolling_median(sig["sat"], int(40 / WIN))) / (np.median(np.abs(sig["sat"] - np.median(sig["sat"]))) * 1.4826 + 1e-6)
    lum_z = (sig["luma"] - _rolling_median(sig["luma"], int(40 / WIN))) / (np.median(np.abs(sig["luma"] - np.median(sig["luma"]))) * 1.4826 + 1e-6)
    # death / kill-cam / respawn screens: the picture goes dim and grey (or washes red) for a few seconds
    # much redder than this stretch of the clip usually is (so a warm grade or a sunset doesn't count)
    red_wash = sig["red"] > np.maximum(40.0, _rolling_median(sig["red"], int(40 / WIN)) * 2.2)
    dead = (((sat_z < -1.5) & (lum_z < -0.8)) | red_wash).astype(np.float32)
    dead = np.clip(_smooth(dead, int(2 / WIN)) * 1.6, 0, 1)
    away = info.screen.penalty(n, WIN) if filmed else np.zeros(n)            # phone pointed away from the TV
    moving_phone = np.clip((_smooth(sig["shake"], int(1 / WIN)) - 0.04) / 0.08, 0, 1)   # phone being repositioned

    avoid = np.clip(np.maximum.reduce([static, dark, dead, away, moving_phone * 0.7]), 0, 1)
    score = score - 3.0 * static - 3.0 * dark - 2.5 * dead - 4.0 * _smooth(away, int(2 / WIN)) - 1.5 * moving_phone
    # a burst that is sustained (crowd roar, multi-kill) beats a single click
    curve = 0.7 * _smooth(score, int(2 / WIN)) + 0.3 * _smooth(score, int(6 / WIN))
    return (curve, avoid) if detail else curve


def _place(peak: float, length: float, dur: float, avoid: np.ndarray) -> float:
    """Start of the window around a payoff: build-up first (payoff ~60 % in), nudged so as little as
    possible of it is a death screen, a loading screen or the phone pointing away."""
    ideal = peak - length * 0.62
    best, best_cost = None, None
    for frac in np.linspace(0.35, 0.85, 11):                # payoff anywhere from 35 % to 85 % through
        s0 = float(min(max(0.0, peak - length * frac), max(0.0, dur - length)))
        i0, i1 = int(s0 / WIN), int((s0 + length) / WIN)
        cost = float(avoid[i0:i1].sum()) + abs(s0 - ideal) * 0.15
        if best_cost is None or cost < best_cost - 1e-9:
            best, best_cost = s0, cost
    return best


def find_moments(video: Path, info: media.VideoInfo, length: float, count: int,
                 min_gap: float) -> list[Moment]:
    dur = info.duration
    if dur <= length + 1:
        return [Moment(peak=dur * 0.6, start=0.0, end=dur, score=0.0)]

    curve, avoid = excitement_curve(video, info, detail=True)
    order = np.argsort(curve)[::-1]
    chosen: list[Moment] = []
    for idx in order:
        t = idx * WIN + WIN / 2
        if t < 3 or t > dur - 1.5:
            continue
        if avoid[idx] > 0.5:                     # the peak itself must be watchable
            continue
        if any(abs(t - m.peak) < min_gap for m in chosen):
            continue
        # refine the payoff to the strongest instant within ±1 s
        lo, hi = max(0, idx - 2), min(len(curve), idx + 3)
        peak = float((lo + int(np.argmax(curve[lo:hi]))) * WIN + WIN / 2)
        start = _place(peak, length, dur, avoid)
        bad = float(avoid[int(start / WIN):int((start + length) / WIN)].mean()) if len(avoid) else 0.0
        if bad > 0.45:                           # most of the window is unwatchable: not a moment
            continue
        if any(min(start + length, m.end) - max(start, m.start) > 0.35 * length for m in chosen):
            continue                             # mostly the same footage as a reel already picked
        chosen.append(Moment(peak=round(peak, 2), start=round(start, 2),
                             end=round(start + length, 2), score=round(float(curve[idx]) - 2 * bad, 3)))
        if len(chosen) >= count:
            break
    chosen.sort(key=lambda m: m.score, reverse=True)
    return chosen
