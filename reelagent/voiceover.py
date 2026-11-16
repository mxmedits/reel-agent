"""AI voiceover for clips that have no commentary of their own (stand-in for music for now).

  1. Claude watches the clip stills and writes 3-6 short commentary lines, each with a start
     time, one landing on the payoff and the last echoing the hook (ig-reel shape: hook, stake,
     body, payoff, loop).
  2. beats.py (ig-reel) times the script at speaking pace; if it doesn't fit the clip, Claude cuts it.
  3. A TTS engine voices each line: piper (free, offline, default), elevenlabs or openai.
  4. Lines are placed on the timeline, game audio is ducked under the voice, and the words
     become the burned-in captions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import requests

from . import media, toolkit
from .captions import _frames, ask_claude, parse_json

SYSTEM = """You write short, punchy voiceover commentary for gaming clips on Instagram Reels, \
spoken by a hype but natural narrator in first person as the page ("I", "we").
Rules:
- Only describe what the stills and context actually show. Never invent kills, scores or events.
- 3 to 6 lines. Each line 2-12 words, said in one breath. Contractions. No em dashes, no emojis.
- Line 1 starts at about 0.3s and is the hook. One line lands right before the payoff time.
- The last line echoes one word from line 1, so the replay loops cleanly.
- Leave silence: total speech well under the clip length. Never talk over the payoff moment itself.
Return ONLY JSON: {"lines": [{"t": <seconds from clip start>, "text": "..."}]}"""


def wanted(cfg, transcript: str) -> bool:
    v = cfg.get("voiceover", {}) or {}
    mode = v.get("mode", "auto")
    if mode == "off" or not cfg.ai_available:
        return False
    return mode == "always" or (mode == "auto" and not transcript.strip())


# ── script ──────────────────────────────────────────────────────────────────

def _fits(lines: list[dict], clip_len: float) -> tuple[bool, float]:
    beats = toolkit._mod("ig-reel", "beats")
    a = beats.analyse("\n".join(l["text"] for l in lines), wpm=165, target=clip_len)
    total = a["total_seconds"] if a else 0
    return total <= clip_len * 0.7, total


def write_script(cfg, video: Path, moment, copy: dict, profile: dict) -> list[dict]:
    clip_len = moment.end - moment.start
    peak = moment.peak - moment.start
    times = [moment.start + clip_len * 0.1, moment.start + clip_len * 0.4, moment.peak - 0.8, moment.peak + 1.2]
    prompt = (f"Game: {profile.get('name')}. Clip length {clip_len:.1f}s. The payoff happens at {peak:.1f}s.\n"
              f"What happens: {copy.get('what_happens') or 'see stills'}\n"
              f"On-screen hook already shown: \"{copy.get('hook_text')}\"\n"
              f"Stills: {clip_len * 0.1:.0f}s, {clip_len * 0.4:.0f}s, just before payoff, just after payoff.")
    with tempfile.TemporaryDirectory() as td:
        content = _frames(video, times, Path(td)) + [{"type": "text", "text": prompt}]
        lines = parse_json(ask_claude(cfg, SYSTEM, content, 700)).get("lines", [])
    lines = _sanitize(lines, clip_len)
    ok, total = _fits(lines, clip_len)
    if not ok and lines:
        fix = (f"This voiceover takes {total:.1f}s to say but the clip is {clip_len:.1f}s and needs silence. "
               f"Cut it to under {clip_len * 0.6:.0f}s of speech, same JSON shape:\n{json.dumps({'lines': lines})}")
        lines = _sanitize(parse_json(ask_claude(cfg, SYSTEM, [{"type": "text", "text": fix}], 500)).get("lines", []),
                          clip_len)
    return [{"t": l["t"], "text": toolkit.humanize(l["text"])[0]} for l in lines]


def _sanitize(lines, clip_len: float) -> list[dict]:
    out = []
    for l in lines or []:
        try:
            t, text = float(l.get("t", 0)), str(l.get("text", "")).strip()
        except (TypeError, ValueError):
            continue
        if text and 0 <= t < clip_len - 1:
            out.append({"t": round(t, 2), "text": text})
    return sorted(out, key=lambda x: x["t"])[:6]


# ── TTS engines ─────────────────────────────────────────────────────────────

def _wav_duration(p: Path) -> float:
    with wave.open(str(p), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _to_wav(src: Path, dst: Path, speed: float = 1.0) -> Path:
    filt = f"atempo={speed}" if abs(speed - 1.0) > 0.01 else "anull"
    media.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-af", filt, "-ac", "1", "-ar", "48000", str(dst)])
    return dst


def _piper(cfg, text: str, out: Path) -> Path:
    v = cfg.get("voiceover", {})
    name = v.get("voice", "en_US-ryan-high")
    vdir = cfg.state_dir / "voices"
    vdir.mkdir(parents=True, exist_ok=True)
    model = vdir / f"{name}.onnx"
    if not model.exists():
        print(f"   downloading Piper voice {name} (one time)...")
        subprocess.run([sys.executable, "-m", "piper.download_voices", "--data-dir", str(vdir), name], check=True)
    from piper import PiperVoice

    voice = _piper_cache.get(str(model)) or PiperVoice.load(str(model))
    _piper_cache[str(model)] = voice
    raw = out.with_suffix(".raw.wav")
    with wave.open(str(raw), "wb") as w:
        voice.synthesize_wav(text, w)
    return _to_wav(raw, out, float(v.get("speed", 1.08)))


_piper_cache: dict = {}


def _elevenlabs(cfg, text: str, out: Path) -> Path:
    v = cfg.get("voiceover", {})
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY missing in .env")
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{v.get('voice')}", timeout=120,
                      headers={"xi-api-key": key, "accept": "audio/mpeg"},
                      json={"text": text, "model_id": v.get("model", "eleven_multilingual_v2")})
    r.raise_for_status()
    mp3 = out.with_suffix(".mp3")
    mp3.write_bytes(r.content)
    return _to_wav(mp3, out, float(v.get("speed", 1.0)))


def _openai(cfg, text: str, out: Path) -> Path:
    v = cfg.get("voiceover", {})
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY missing in .env")
    r = requests.post("https://api.openai.com/v1/audio/speech", timeout=120,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": v.get("model", "gpt-4o-mini-tts"), "voice": v.get("voice", "onyx"),
                            "input": text, "response_format": "wav"})
    r.raise_for_status()
    raw = out.with_suffix(".raw.wav")
    raw.write_bytes(r.content)
    return _to_wav(raw, out, float(v.get("speed", 1.0)))


def _flite(cfg, text: str, out: Path) -> Path:
    """Robotic offline voice built into some ffmpeg builds. Only for testing."""
    safe = text.replace("'", "").replace(":", " ").replace("\\", "")
    media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"flite=text='{safe}'",
               "-ac", "1", "-ar", "48000", str(out)])
    return out


ENGINES = {"piper": _piper, "elevenlabs": _elevenlabs, "openai": _openai, "flite": _flite}


def engine_ready(cfg) -> tuple[bool, str]:
    eng = (cfg.get("voiceover", {}) or {}).get("engine", "piper")
    if eng == "piper":
        try:
            import piper  # noqa: F401
            return True, "piper"
        except Exception:
            return False, "piper-tts not installed (pip install piper-tts)"
    if eng == "elevenlabs":
        return bool(os.getenv("ELEVENLABS_API_KEY")), "elevenlabs"
    if eng == "openai":
        return bool(os.getenv("OPENAI_API_KEY")), "openai"
    return eng in ENGINES, eng


# ── timeline ────────────────────────────────────────────────────────────────

def build(cfg, lines: list[dict], clip_len: float, out_wav: Path) -> list[dict]:
    """Voice every line, place it, write one aligned track to out_wav. Returns caption words."""
    eng = ENGINES[(cfg.get("voiceover", {}) or {}).get("engine", "piper")]
    placed, words = [], []
    with tempfile.TemporaryDirectory() as td:
        cursor = 0.0
        for i, l in enumerate(lines):
            wav = eng(cfg, l["text"], Path(td) / f"l{i}.wav")
            dur = _wav_duration(wav)
            t = max(l["t"], cursor + 0.15)
            if t + dur > clip_len - 0.2:
                break
            placed.append((t, wav))
            cursor = t + dur
            toks = l["text"].split()
            weights = [max(len(w), 2) for w in toks]
            acc, total = t, float(sum(weights))
            for w, wt in zip(toks, weights):
                span = dur * wt / total
                words.append({"w": w, "s": round(acc, 3), "e": round(acc + span, 3)})
                acc += span
        if not placed:
            return []
        cmd = ["ffmpeg", "-y", "-v", "error"]
        for _, wav in placed:
            cmd += ["-i", str(wav)]
        chains = [f"[{i}:a]adelay={int(t * 1000)}|{int(t * 1000)}[d{i}]" for i, (t, _) in enumerate(placed)]
        mix = "".join(f"[d{i}]" for i in range(len(placed)))
        n = len(placed)
        graph = ";".join(chains) + f";{mix}amix=inputs={n}:duration=longest:dropout_transition=0,volume={n}," \
                                   f"apad=whole_dur={clip_len:.3f}[v]"
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        media.run(cmd + media.graph_args(graph, Path(td), "vo_graph.txt")
                  + ["-map", "[v]", "-ac", "1", "-ar", "48000", "-t", f"{clip_len:.3f}", str(out_wav.resolve())],
                  cwd=Path(td))
    return words
