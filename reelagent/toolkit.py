"""Bridge to the vendored instagram-agent-skill pack (MIT, Jake Schincariol).

vendor/instagram-agent-skill/ is the full pack, pinned to a reviewed commit.
The agent uses its local, offline tools on every post:
  ig-reel   hookscore.py + hooks.json   score/choose the on-screen hook, classify formulas
  ig-human  humanize.py + detect.py     strip the AI fingerprint from captions and replies
  ig-caption caption.py                 lint the caption (125-char window, 5-hashtag cap)
  ig-viral  swipe.py                    outlier-multiple ranking + formula naming (used by the audit)
  ig-profile rubric.json                100-point profile rubric
and feeds the SKILL.md playbooks (ig-reply, ig-audit, ig-profile, ig-plan...) to Claude as instructions.
"""
from __future__ import annotations

import importlib.util
import json
import re
from functools import lru_cache
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent / "vendor" / "instagram-agent-skill"
SKILLS = PACK / "skills"


@lru_cache(maxsize=None)
def _mod(skill: str, name: str):
    path = SKILLS / skill / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"igpack_{skill.replace('-', '_')}_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@lru_cache(maxsize=1)
def _lexicon():
    return _mod("ig-human", "humanize").load_lexicon(str(SKILLS / "ig-human" / "slop.json"))


def available() -> bool:
    return (SKILLS / "ig-reel" / "hookscore.py").exists()


# ── hooks ──────────────────────────────────────────────────────────────────

def score_hook(text: str) -> tuple[float, str]:
    _, overall, verdict, _ = _mod("ig-reel", "hookscore").run(text)
    return round(overall, 1), verdict


@lru_cache(maxsize=1)
def hook_formulas() -> list[dict]:
    d = json.loads((SKILLS / "ig-reel" / "hooks.json").read_text(encoding="utf-8"))
    return d["hooks"]


def formula_menu(limit: int = 26) -> str:
    """Compact list for prompts: '#3 Nobody Tells You → YOUR FIRST 30 WILL FLOP'."""
    return "\n".join(f"#{h['id']} {h['name']}: e.g. on-screen \"{h['on_screen']}\""
                     for h in hook_formulas()[:limit])


def classify_hook(text: str) -> tuple[int | None, str]:
    sw = _mod("ig-viral", "swipe")
    formulas = sw.load_formulas(str(SKILLS / "ig-reel" / "hooks.json"))
    return sw.classify(text, formulas)


def best_hook(options: list[str]) -> tuple[str, float, str, list]:
    """Score every option with hookscore.py and return the winner (+ the table)."""
    table = []
    for i, h in enumerate(o for o in options if o and o.strip()):
        s, v = score_hook(h)
        table.append({"hook": h.strip(), "score": s, "verdict": v, "rank_in": i})
    if not table:
        return "", 0.0, "WEAK", []
    table.sort(key=lambda r: (-r["score"], r["rank_in"]))
    w = table[0]
    return w["hook"], w["score"], w["verdict"], table


# ── captions / replies ─────────────────────────────────────────────────────

def humanize(text: str) -> tuple[str, dict]:
    clean, report = _mod("ig-human", "humanize").humanize(text, _lexicon())
    return clean.strip(), report


def human_score(text: str) -> tuple[float, str]:
    _, overall, verdict = _mod("ig-human", "detect").run(text, _lexicon())
    return round(overall, 1), verdict


def lint_caption(text: str, keywords: list[str] | None = None) -> dict:
    return _mod("ig-caption", "caption").analyse(text, keywords=keywords or [])


def lint_problems(report: dict) -> list[str]:
    return [f"{c['check']}: {c['detail']}" for c in report.get("checks", [])
            if c.get("status") in ("FAIL", "WARN")]


# ── playbooks ──────────────────────────────────────────────────────────────

def playbook(skill: str) -> str:
    """The SKILL.md body (without front matter) to use as instructions for Claude."""
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    return re.sub(r"^---.*?---\s*", "", text, flags=re.S).strip()


def profile_rubric() -> dict:
    return json.loads((SKILLS / "ig-profile" / "rubric.json").read_text(encoding="utf-8"))
