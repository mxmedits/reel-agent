"""Optional speech-to-text for burned-in captions (uses faster-whisper if installed)."""
from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from . import media

# Whisper sometimes "hears" these in pure game audio
_HALLUCINATIONS = {
    "thank you for watching", "thanks for watching", "subscribe", "please subscribe",
    "you", "thank you", "bye", "music", "applause",
}


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


@lru_cache(maxsize=2)
def _model(size: str):
    from faster_whisper import WhisperModel

    return WhisperModel(size, device="auto", compute_type="int8")


def words(video: Path, start: float, dur: float, model_size: str = "small") -> list[dict]:
    """Return [{'w': 'word', 's': sec, 'e': sec}] relative to `start`. [] if no clear speech."""
    if not available():
        return []
    with tempfile.TemporaryDirectory() as td:
        wav = media.extract_wav(video, start, dur, Path(td) / "a.wav")
        segments, _ = _model(model_size).transcribe(
            str(wav), word_timestamps=True, vad_filter=True, beam_size=5,
            condition_on_previous_text=False,
        )
        out = []
        for seg in segments:
            text = seg.text.strip().lower().strip(".!? ")
            if seg.no_speech_prob > 0.6 or seg.avg_logprob < -1.0 or text in _HALLUCINATIONS:
                continue
            for w in seg.words or []:
                word = w.word.strip()
                if word:
                    out.append({"w": word, "s": float(w.start), "e": float(w.end)})
        return out
