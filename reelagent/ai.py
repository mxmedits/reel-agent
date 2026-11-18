"""One door to the AI brain, whichever provider you use.

  claude  : Anthropic API (best quality, pay per use)
  gemini  : Google Gemini API free tier (free, rate-limited; Google may use free-tier data to improve its products)
  none    : no AI at all. Templates write the hooks/captions; no quality filter, replies or voiceover scripts.
ai.provider "auto" picks claude if ANTHROPIC_API_KEY is set, else gemini if GEMINI_API_KEY is set, else none.
Content blocks use one shape everywhere: {"type": "text", "text": ...} and
{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": ...}}.
"""
from __future__ import annotations

import os
import threading
import time

import requests


class AIUnavailable(RuntimeError):
    pass


def provider(cfg) -> str:
    want = (cfg.get("ai") or {}).get("provider", "auto")
    if want == "claude":
        return "claude" if os.getenv("ANTHROPIC_API_KEY") else "none"
    if want == "gemini":
        return "gemini" if os.getenv("GEMINI_API_KEY") else "none"
    if want == "none":
        return "none"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "claude"
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    return "none"


def available(cfg) -> bool:
    return provider(cfg) != "none"


def ask(cfg, system: str, content: list, max_tokens: int = 1200, search: bool = False) -> str:
    p = provider(cfg)
    if p == "claude":
        return _claude(cfg, system, content, max_tokens, search)
    if p == "gemini":
        return _gemini(cfg, system, content, max_tokens, search)
    raise AIUnavailable("no AI provider configured")


# ── Claude ──────────────────────────────────────────────────────────────────

def _claude(cfg, system, content, max_tokens, search):
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    model = (cfg.get("ai") or {}).get("model", "claude-sonnet-5-5")
    if not search:
        msg = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                     messages=[{"role": "user", "content": content}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    last = None
    for tool_type in ("web_search_20260318", "web_search_20250305"):   # newest first, then the basic tool
        try:
            messages = [{"role": "user", "content": content}]
            tools = [{"type": tool_type, "name": "web_search", "max_uses": 8}]
            resp = None
            for _ in range(4):                 # server tools may pause a long turn; continue it
                resp = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                              messages=messages, tools=tools)
                if getattr(resp, "stop_reason", "") != "pause_turn":
                    break
                messages = messages + [{"role": "assistant", "content": resp.content}]
            return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        except Exception as e:
            last = e
    raise AIUnavailable(f"Claude web search unavailable: {last}")


# ── Gemini (free tier friendly) ─────────────────────────────────────────────

_gem_lock = threading.Lock()
_gem_last = [0.0]
GEMINI_MIN_GAP = 7.0          # seconds between calls: stays under free-tier requests-per-minute limits


def _gemini_parts(content: list) -> list:
    parts = []
    for b in content:
        if b.get("type") == "text":
            parts.append({"text": b["text"]})
        elif b.get("type") == "image":
            src = b["source"]
            parts.append({"inline_data": {"mime_type": src.get("media_type", "image/jpeg"), "data": src["data"]}})
    return parts


def _gemini(cfg, system, content, max_tokens, search):
    key = os.getenv("GEMINI_API_KEY")
    model = (cfg.get("ai") or {}).get("gemini_model", "gemini-flash-lite-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": _gemini_parts(content)}],
            "generationConfig": {"maxOutputTokens": max(max_tokens, 1024), "temperature": 0.8}}
    attempts = [True, False] if search else [False]   # try Google Search grounding, then without it
    last = None
    for use_search in attempts:
        b = dict(body)
        if use_search:
            b["tools"] = [{"google_search": {}}]
        for retry in range(3):
            with _gem_lock:
                wait = GEMINI_MIN_GAP - (time.time() - _gem_last[0])
                if wait > 0:
                    time.sleep(wait)
                _gem_last[0] = time.time()
            r = requests.post(url, json=b, timeout=120, headers={"x-goog-api-key": key})
            if r.status_code == 429 and retry == 0 and "per day" not in r.text.lower():
                time.sleep(35)                 # per-minute limit: wait it out once
                continue
            if r.status_code in (500, 502, 503, 504) and retry < 2:
                time.sleep(8 * (retry + 1))    # "high demand" spikes pass within seconds
                continue
            break
        if r.status_code == 200:
            data = r.json()
            cands = data.get("candidates") or []
            if cands:
                parts = (cands[0].get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts)
                if text.strip():
                    return text
            last = f"empty answer ({(data.get('promptFeedback') or {}).get('blockReason', 'no text')})"
        else:
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code == 429 and not use_search:
                raise AIUnavailable(f"Gemini free-tier limit reached for now ({last})")
    raise AIUnavailable(f"Gemini: {last}")
