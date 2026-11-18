"""Background music the agent makes itself, so every reel can have music even with an empty library.

Each beat is built for one reel: the tempo and sound follow the mood the AI picked, the build-up runs
under the start of the clip, and the drop (a big hit, then the full beat) lands exactly on the payoff.
Everything is synthesised here from sine waves and noise, so there is nothing to license: it is
safe to post. Tracks you add yourself (Settings, Music) and Instagram's trending sounds are used
first when they are available; these beats fill in when they aren't.

Moods: hype (trap), intense (dark drill-style), epic (cinematic drums and strings), chill (lo-fi),
funny (bouncy plucks), suspense (pulses and a hit).
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 44100
A4 = 440.0

STYLE = {   # bpm, swing, key root (semitones from A), scale, progression (scale degrees, 0-based)
    "hype":     dict(bpm=140, half=True, swing=0.0, scale="minor", prog=[0, 5, 2, 6], drums="trap", lead="pluck"),
    "intense":  dict(bpm=142, half=True, swing=0.0, scale="phrygian", prog=[0, 1, 0, 5], drums="drill", lead="bell"),
    "epic":     dict(bpm=96, half=False, swing=0.0, scale="minor", prog=[0, 5, 2, 6], drums="epic", lead="strings"),
    "chill":    dict(bpm=82, half=False, swing=0.18, scale="dorian", prog=[0, 3, 6, 2], drums="lofi", lead="keys"),
    "funny":    dict(bpm=116, half=False, swing=0.12, scale="major", prog=[0, 3, 4, 0], drums="bounce", lead="pizz"),
    "suspense": dict(bpm=92, half=False, swing=0.0, scale="minor", prog=[0, 0, 5, 4], drums="pulse", lead="tremolo"),
}
SCALES = {"minor": [0, 2, 3, 5, 7, 8, 10], "major": [0, 2, 4, 5, 7, 9, 11], "dorian": [0, 2, 3, 5, 7, 9, 10],
          "phrygian": [0, 1, 3, 5, 7, 8, 10]}


def _hz(semi: float) -> float:
    return A4 * 2 ** (semi / 12)


def _t(n: int) -> np.ndarray:
    return np.arange(n) / SR


def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """Zero-phase low-pass through the FFT (gentle slope)."""
    n = len(x)
    if n < 16 or cutoff >= SR / 2:
        return x
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / SR)
    spec *= 1 / np.sqrt(1 + (f / cutoff) ** 4)
    return np.fft.irfft(spec, n)


def _highpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    return x - _lowpass(x, cutoff)


class Mix:
    def __init__(self, seconds: float):
        self.n = int(seconds * SR) + SR
        self.buses = {k: np.zeros(self.n) for k in ("drums", "bass", "music", "fx")}

    def add(self, bus: str, start: float, sig: np.ndarray, gain: float = 1.0) -> None:
        i = int(start * SR)
        if i >= self.n or i + len(sig) <= 0:
            return
        if i < 0:
            sig, i = sig[-i:], 0
        j = min(self.n, i + len(sig))
        self.buses[bus][i:j] += sig[: j - i] * gain


# ── instruments ──────────────────────────────────────────────────────────────

def kick(soft: bool = False) -> np.ndarray:
    n = int(0.45 * SR); t = _t(n)
    f = 46 + (95 if soft else 130) * np.exp(-t * 32)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * (9 if soft else 6.5))
    click = np.random.default_rng(1).standard_normal(n) * np.exp(-t * 350) * (0.12 if soft else 0.3)
    return np.tanh((body + click) * (1.4 if soft else 2.2)) * 0.95


def snare(clap: bool = False, rng=None) -> np.ndarray:
    rng = rng or np.random.default_rng(2)
    n = int(0.32 * SR); t = _t(n)
    noise = _highpass(rng.standard_normal(n), 1400)
    if clap:      # three quick bursts, like hands
        envs = sum(np.exp(-np.clip(t - d, 0, None) * 70) * (t >= d) for d in (0, 0.011, 0.023))
        return np.tanh(noise * envs * 0.55 + noise * np.exp(-t * 16) * 0.35) * 0.8
    tone = np.sin(2 * np.pi * 190 * t) * np.exp(-t * 28) * 0.6
    return np.tanh(noise * np.exp(-t * 20) * 0.7 + tone) * 0.75


def hat(open_: bool = False, rng=None) -> np.ndarray:
    rng = rng or np.random.default_rng(3)
    n = int((0.35 if open_ else 0.07) * SR); t = _t(n)
    x = _highpass(rng.standard_normal(n), 7000)
    return x * np.exp(-t * (11 if open_ else 75)) * 0.45


def tom(pitch: float = 70) -> np.ndarray:
    n = int(0.9 * SR); t = _t(n)
    f = pitch + pitch * 0.8 * np.exp(-t * 18)
    return np.tanh(np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 4.5) * 1.8) * 0.9


def sub808(freq: float, dur: float, glide_from: float | None = None) -> np.ndarray:
    n = int(dur * SR); t = _t(n)
    f = np.full(n, freq)
    if glide_from:
        f = freq + (glide_from - freq) * np.exp(-t * 22)
    x = np.sin(2 * np.pi * np.cumsum(f) / SR)
    env = np.minimum(1, t / 0.004) * np.exp(-t * 1.6)
    return np.tanh(x * env * 2.4) * 0.8


def saw(freq: float, n: int, detune: float = 0.0, phase: float = 0.0) -> np.ndarray:
    f = freq * (1 + detune)
    return 2 * ((_t(n) * f + phase) % 1.0) - 1


def pad(freqs: list[float], dur: float, attack: float = 0.25, bright: float = 2200) -> np.ndarray:
    n = int(dur * SR); t = _t(n)
    x = np.zeros(n)
    rng = np.random.default_rng(int(sum(freqs)))
    for f in freqs:
        for d in (-0.007, 0.0, 0.006):
            x += saw(f, n, d, rng.random())
    x = _lowpass(x / (3 * len(freqs)), bright)
    env = np.minimum(1, t / attack) * np.minimum(1, (dur - t) / 0.12).clip(0)
    return x * env * 0.5


def pluck(freq: float, dur: float = 0.35, bright: float = 4500, decay: float = 9.0) -> np.ndarray:
    n = int(dur * SR); t = _t(n)
    x = 0.6 * saw(freq, n) + 0.4 * np.sign(np.sin(2 * np.pi * freq * t))
    return _lowpass(x, bright) * np.exp(-t * decay) * 0.35


def bell(freq: float, dur: float = 0.8) -> np.ndarray:
    n = int(dur * SR); t = _t(n)
    x = np.sin(2 * np.pi * freq * t + 1.8 * np.sin(2 * np.pi * freq * 3.5 * t) * np.exp(-t * 6))
    return x * np.exp(-t * 4) * 0.25


def keys(freqs: list[float], dur: float) -> np.ndarray:
    n = int(dur * SR); t = _t(n)
    x = sum(np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t) for f in freqs) / len(freqs)
    trem = 1 + 0.12 * np.sin(2 * np.pi * 4.5 * t)
    return x * trem * np.exp(-t * 1.2) * np.minimum(1, t / 0.01) * 0.32


def riser(dur: float, rng=None) -> np.ndarray:
    rng = rng or np.random.default_rng(4)
    n = int(dur * SR); t = _t(n)
    x = rng.standard_normal(n)
    out = np.zeros(n)
    k = 8
    for i in range(k):                       # noise that opens up as it rises
        a, b = i * n // k, (i + 1) * n // k
        out[a:b] = _highpass(_lowpass(x[a:b], 1500 + 9000 * (i + 1) / k), 300)
    return out * (t / dur) ** 2 * 0.35


def impact(rng=None) -> np.ndarray:
    rng = rng or np.random.default_rng(5)
    n = int(2.2 * SR); t = _t(n)
    boom = np.sin(2 * np.pi * np.cumsum(38 + 60 * np.exp(-t * 6)) / SR) * np.exp(-t * 2.2)
    crash = _highpass(rng.standard_normal(n), 4000) * np.exp(-t * 2.6) * 0.3
    return np.tanh(boom * 2.0) * 0.9 + crash


# ── arranging ────────────────────────────────────────────────────────────────

def make(mood: str, seconds: float, drop_at: float, out: Path, seed: int = 0) -> Path:
    """Write a WAV of `seconds` whose drop lands at `drop_at` seconds."""
    st = STYLE.get(mood, STYLE["hype"])
    rng = np.random.default_rng(seed)
    root = -12 + int(rng.choice([0, 2, 3, 5, 7, -2]))          # key around A
    scale = SCALES[st["scale"]]
    step = 60.0 / st["bpm"] / 4                                 # 16th note at the real bpm
    bar = step * 16
    drop_at = float(min(max(1.0, drop_at), max(1.0, seconds - 2)))
    mix = Mix(seconds)
    drums = st["drums"]

    def note(deg: int, octave: int = 0) -> float:
        d = deg % 7
        return _hz(root + scale[d] + 12 * (octave + deg // 7))

    def chord(deg: int, octave: int = 0) -> list[float]:
        return [note(deg, octave), note(deg + 2, octave), note(deg + 4, octave)]

    # the grid is anchored on the drop, so the build-up is on the beat too
    first_bar = drop_at - bar * np.ceil(drop_at / bar)
    n_bars = int(np.ceil((seconds - first_bar) / bar)) + 1
    for b in range(n_bars):
        t0 = first_bar + b * bar
        after = t0 >= drop_at - 1e-6
        deg = st["prog"][b % len(st["prog"])]
        if mood in ("chill",):
            mix.add("music", t0, keys(chord(deg, 1), bar * 0.98), 1.6 if after else 1.4)
        elif mood == "funny":
            pass
        else:
            mix.add("music", t0, pad(chord(deg, 0 if mood != "epic" else -1), bar, attack=0.4 if mood == "epic" else 0.2,
                                     bright=2600 if after else 1500), 0.9 if after else 1.1)
        for s in range(16):
            ts = t0 + s * step + (st["swing"] * step if s % 2 else 0)
            if ts < -0.5 or ts > seconds:
                continue
            # melody / lead
            if st["lead"] == "pluck" and after and s % 2 == 0 and rng.random() < 0.55:
                mix.add("music", ts, pluck(note(deg + int(rng.choice([0, 2, 4, 7])), 2)), 0.8)
            elif st["lead"] == "bell" and s in (0, 3, 6, 10, 12) and (after or rng.random() < 0.4):
                mix.add("music", ts, bell(note(deg + int(rng.choice([0, 2, 4])), 2)), 0.9)
            elif st["lead"] == "pizz" and s % 2 == 0 and rng.random() < 0.7:
                mix.add("music", ts, pluck(note(deg + int(rng.choice([0, 2, 4, 5, 7])), 1), 0.18, 3500, 22), 1.1)
            elif st["lead"] == "tremolo" and after and s % 2 == 0:
                mix.add("music", ts, pluck(note(deg + 4, 2), 0.12, 2500, 30), 0.45)
            if not after:
                # build-up: hats only, getting busier, and a riser in the last two bars
                if drums in ("trap", "drill") and s % 2 == 0:
                    mix.add("drums", ts, hat(rng=rng), 0.5 if s % 4 else 0.7)
                if drums == "lofi" and s % 4 == 2:
                    mix.add("drums", ts, hat(rng=rng), 0.4)
                if drums == "pulse" and s % 2 == 0:
                    mix.add("bass", ts, sub808(note(deg, -2), step * 1.6), 0.5)
                if drums == "epic" and s in (0, 6, 8):
                    mix.add("drums", ts, tom(55), 0.5)
                if drums == "bounce" and s % 4 == 0:
                    mix.add("drums", ts, kick(soft=True), 0.55)
                continue
            if drums == "trap":
                if s in (0, 7, 10) or (s == 14 and rng.random() < 0.4):
                    mix.add("drums", ts, kick(), 1.0)
                    mix.add("bass", ts, sub808(note(deg, -2), step * (6 if s == 0 else 3)), 0.85)
                if s == 8:
                    mix.add("drums", ts, snare(clap=True, rng=rng), 0.9)
                if s % 2 == 0:
                    mix.add("drums", ts, hat(rng=rng), 0.55)
                if s in (13, 15) and rng.random() < 0.5:                     # hi-hat rolls
                    for r in range(3):
                        mix.add("drums", ts + r * step / 3, hat(rng=rng), 0.35)
            elif drums == "drill":
                if s in (0, 10) or (s == 13 and b % 2):
                    mix.add("drums", ts, kick(), 0.95)
                    mix.add("bass", ts, sub808(note(deg, -2), step * 5, glide_from=note(deg + 1, -2)), 0.9)
                if s in (4, 12) if b % 2 else s in (8,):
                    mix.add("drums", ts, snare(rng=rng), 0.85)
                if s % 2 == 0 or s in (3, 11):
                    mix.add("drums", ts, hat(rng=rng), 0.5)
            elif drums == "epic":
                if s in (0, 3, 6, 8, 11, 14):
                    mix.add("drums", ts, tom(48 if s in (0, 8) else 70), 1.0 if s in (0, 8) else 0.7)
                if s in (4, 12):
                    mix.add("drums", ts, snare(rng=rng), 0.6)
                if s == 0:
                    mix.add("bass", ts, sub808(note(deg, -2), bar * 0.9), 0.6)
            elif drums == "lofi":
                if s in (0, 7, 10):
                    mix.add("drums", ts, kick(soft=True), 0.8)
                if s in (4, 12):
                    mix.add("drums", ts, snare(rng=rng), 0.45)
                if s % 2 == 0:
                    mix.add("drums", ts, hat(rng=rng), 0.35)
                if s == 0:
                    mix.add("bass", ts, sub808(note(deg, -1), bar * 0.45), 0.5)
            elif drums == "bounce":
                if s in (0, 8):
                    mix.add("drums", ts, kick(), 0.85)
                if s in (4, 12):
                    mix.add("drums", ts, snare(clap=True, rng=rng), 0.75)
                if s % 2 == 1:
                    mix.add("drums", ts, hat(rng=rng), 0.4)
                if s in (0, 6, 8, 14):
                    mix.add("bass", ts, pluck(note(deg, -1), 0.22, 900, 12), 1.6)
            elif drums == "pulse":
                if s % 2 == 0:
                    mix.add("bass", ts, sub808(note(deg, -2), step * 1.5), 0.6)
                if s in (0, 8):
                    mix.add("drums", ts, kick(soft=True), 0.8)
                if s % 4 == 2:
                    mix.add("drums", ts, hat(rng=rng), 0.3)
    # the drop: a riser into it, a hit on it
    rise = min(drop_at, bar * 2)
    if rise > 0.4:
        mix.add("fx", drop_at - rise, riser(rise, rng), 0.8)
    mix.add("fx", drop_at, impact(rng), 0.9)

    # sidechain: music and bass dip under each kick, which is what makes it "pump"
    env = np.ones(mix.n)
    k = np.abs(mix.buses["drums"])
    k = _lowpass(k, 25)
    env = 1 - 0.45 * np.clip(k / (np.percentile(k, 99.5) + 1e-9), 0, 1)
    music = mix.buses["music"] * env
    bass = mix.buses["bass"] * (0.5 + 0.5 * env)
    left = mix.buses["drums"] + bass + music * 1.0 + mix.buses["fx"]
    right = mix.buses["drums"] + bass + np.roll(music, int(0.011 * SR)) + mix.buses["fx"]   # a little width
    if mood == "chill":                                         # lo-fi: dull the top, add crackle
        left, right = _lowpass(left, 5200), _lowpass(right, 5200)
        crackle = (rng.random(mix.n) > 0.9993) * rng.standard_normal(mix.n) * 0.25
        left, right = left + crackle, right + crackle
    stereo = np.stack([left, right], axis=1)[: int(seconds * SR)]
    # fade in/out and level
    n = len(stereo)
    fade = np.ones(n)
    fi, fo = int(0.05 * SR), int(0.6 * SR)
    fade[:fi] = np.linspace(0, 1, fi)
    fade[-fo:] = np.linspace(1, 0, fo)
    stereo *= fade[:, None]
    stereo = np.tanh(stereo / (np.percentile(np.abs(stereo), 99.9) + 1e-9) * 1.2) * 0.89
    out = Path(out)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((stereo * 32767).astype("<i2").tobytes())
    return out
