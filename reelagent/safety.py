"""Keeps keys and tokens out of anything the agent writes down.

In GitHub mode the agent's records (log, queue, reports) live in a public branch, so every line that
is logged or stored goes through `scrub()` first, and `leaks()` checks the files once more before
they are saved. Error messages from web requests are the usual way a token ends up in a log: the
failing URL is quoted, and Instagram's URLs carry the access token.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

SECRET_ENVS = (
    "IG_ACCESS_TOKEN", "IG_APP_SECRET", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "YOUTUBE_API_KEY",
    "ELEVENLABS_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "NTFY_TOPIC", "DASHBOARD_PASSWORD",
)

_PATTERNS = [
    (re.compile(r"((?:access_token|fb_exchange_token|client_secret|input_token|key|token|api_key)=)[^&\s\"'<>)]+", re.I),
     r"\1***"),
    (re.compile(r"((?:Bearer|OAuth|basic)\s+)[A-Za-z0-9._\-+/=]{12,}", re.I), r"\1***"),
    (re.compile(r"\bEAA[A-Za-z0-9]{24,}"), "***"),                 # Meta access tokens
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "***"),             # Google API keys
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), "***"),          # Anthropic keys
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "***"),
]


def secret_values() -> list[str]:
    vals = {os.environ.get(k, "") for k in SECRET_ENVS}
    return sorted((v for v in vals if len(v) >= 8), key=len, reverse=True)


def scrub(text) -> str:
    """Text with every known secret value and token-shaped string replaced by ***."""
    s = str(text)
    for v in secret_values():
        if v in s:
            s = s.replace(v, "***")
    for pat, rep in _PATTERNS:
        s = pat.sub(rep, s)
    return s


def scrub_obj(obj):
    """scrub() applied to every string inside a JSON-like structure."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v) for v in obj]
    return obj


def leaks(root: Path, skip_dirs: tuple[str, ...] = (".git",)) -> list[str]:
    """Files under root that contain a real secret value (checked byte for byte). Empty list = clean."""
    vals = [v.encode() for v in secret_values()]
    if not vals:
        return []
    bad = []
    for p in Path(root).rglob("*"):
        if not p.is_file() or any(part in skip_dirs for part in p.relative_to(root).parts):
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if any(v in data for v in vals):
            bad.append(str(p.relative_to(root)))
    return bad
