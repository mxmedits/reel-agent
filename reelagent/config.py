"""Loads config.yaml (falls back to config.example.yaml) and .env secrets."""
from __future__ import annotations

import copy
import os
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the code

try:
    import yaml
except ImportError:   # iPhone (a-Shell) can't build PyYAML's C part: use the bundled pure-Python copy
    sys.path.insert(0, str(ROOT / "vendor" / "pyyaml"))
    import yaml


def is_ios() -> bool:
    try:
        machine = os.uname().machine
    except AttributeError:
        machine = platform.machine()
    return (machine.startswith(("iPhone", "iPad", "iPod"))
            or "a-Shell" in os.environ.get("APPDIR", "") or bool(os.environ.get("REELAGENT_IOS")))


def _default_home() -> Path:
    if os.getenv("REELAGENT_HOME"):
        return Path(os.environ["REELAGENT_HOME"]).expanduser()
    if is_ios():   # keep your data outside the code folder so updates never touch it
        return Path.home() / "Documents" / "reelagent-data"
    return ROOT


HOME = _default_home().resolve()   # your data: config.yaml, .env, inbox, output, music
HOME.mkdir(parents=True, exist_ok=True)


def _read_env_file(path: Path) -> None:
    """Minimal .env reader for when python-dotenv isn't installed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        os.environ[k] = v


def load_env() -> None:
    try:  # secrets from .env (re-read so dashboard changes apply without a restart)
        from dotenv import load_dotenv

        load_dotenv(HOME / ".env", override=True)
    except ImportError:
        _read_env_file(HOME / ".env")
    except Exception:  # pragma: no cover
        pass


load_env()


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "games":
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: str | os.PathLike | None = None):
        example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        chosen = Path(path) if path else HOME / "config.yaml"
        user = {}
        if chosen.exists():
            user = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
        self.source = chosen if chosen.exists() else ROOT / "config.example.yaml"
        data = _deep_merge(example, user)
        if "games" in user:  # user game list replaces example, but keep a default profile
            data["games"] = {**{"default": example["games"]["default"]}, **user["games"]}
        self.data = data

        self.inbox = self._path(data["inbox"])
        self.output = self._path(data["output"])
        self.state_dir = self.output / "state"
        for d in (self.inbox, self.output, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _path(self, p: str) -> Path:
        p = Path(p).expanduser()
        return p if p.is_absolute() else (HOME / p).resolve()

    def user_config(self) -> dict:
        """Only what the user changed (what config.yaml holds)."""
        f = HOME / "config.yaml"
        return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.exists() else {}

    @staticmethod
    def save_user_config(data: dict) -> None:
        (HOME / "config.yaml").write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                                          encoding="utf-8")

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    # secrets ---------------------------------------------------------------
    @property
    def anthropic_key(self) -> str | None:
        return os.getenv("ANTHROPIC_API_KEY") or None

    @property
    def ai_available(self) -> bool:
        """True if any AI brain (Claude or free Gemini) is configured."""
        from .ai import available
        return available(self)

    @property
    def ig_user_id(self) -> str | None:
        return os.getenv("IG_USER_ID") or None

    @property
    def ig_token(self) -> str | None:
        return os.getenv("IG_ACCESS_TOKEN") or None

    # games -----------------------------------------------------------------
    def game_profile(self, key: str) -> dict:
        games = self.data["games"]
        prof = dict(games.get(key) or games["default"])
        prof["key"] = key if key in games else "default"
        return prof

    def detect_game(self, video_path: Path) -> str:
        """Folder name wins (inbox/gta6/clip.mp4), then keywords in the path."""
        games = self.data["games"]
        try:
            rel = video_path.resolve().relative_to(self.inbox)
            if len(rel.parts) > 1 and rel.parts[0].lower() in games:
                return rel.parts[0].lower()
        except ValueError:
            pass
        hay = str(video_path).lower().replace("-", " ").replace(".", " ")
        best, best_len = "default", 0
        for key, prof in games.items():
            for kw in [key, *prof.get("match", [])]:
                kw = kw.lower()
                if kw and kw in hay and len(kw) > best_len:
                    best, best_len = key, len(kw)
        return best


def update_env(values: dict) -> Path:
    """Write/replace KEY=VALUE lines in .env."""
    env = HOME / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    keys = set(values)
    kept = [l for l in lines if l.split("=", 1)[0].strip() not in keys]
    kept += [f"{k}={v}" for k, v in values.items()]
    env.write_text("\n".join(kept) + "\n", encoding="utf-8")
    for k, v in values.items():
        os.environ[k] = str(v)
    return env
