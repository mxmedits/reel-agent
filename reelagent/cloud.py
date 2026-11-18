"""GitHub mode: GitHub's free computers run the agent, the app page is the remote control.

  The app (docs/index.html, served by GitHub Pages) uploads a clip in small parts to a branch named
  inbox/<id>, drops command files in the records branch, and starts the workflow.
  The workflow (.github/workflows/agent.yml) runs `python -m reelagent cloud` every 15 minutes and on demand:

    1. load the agent's records (branch `agent-data`: settings, queue, database, covers, reports)
    2. apply commands from the app (settings, post now, skip, caption edits)
    3. refresh trend research if it is due
    4. put uploaded clips back together, edit them into reels, keep the full-quality reels as release
       files (tag `reels`) and a small preview next to the cover
    5. post a queued reel if a posting time has come (it catches up after a late start), then answer
       comments, read Instagram's top posts, learn from the stats
    6. write status.json for the app, send alerts, save the records back

The records branch is public, like everything in a free public repository, so every line written there
goes through safety.scrub() and the files are checked for keys once more before they are saved.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import safety
from .config import HOME, ROOT

DATA_BRANCH = "agent-data"
REELS_TAG = "reels"
INBOX_PREFIX = "inbox/"
ALERT_ISSUE = "Reel agent alerts"
API = "https://api.github.com"
EARLY = timedelta(minutes=7)        # a run this close before a posting time posts now rather than 15 min late
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts", ".flv"}
SETTINGS_KEYS = {"handle", "timezone", "schedule", "autopilot", "clips", "voiceover", "instagram", "engagement",
                 "trends", "review", "music", "phone", "ig_username"}

GITIGNORE = """# the agent's records. Never keys, never full-size videos.
.env
inbox/
music/
output/clips/*/reel.mp4
output/clips/*/voiceover.wav
output/clips/*/voiceover.raw.wav
output/clips/*/beat.wav
output/state/voices/
output/state/*.db-wal
output/state/*.db-shm
"""


# ── git + GitHub plumbing ───────────────────────────────────────────────────

def repo() -> str:
    return os.getenv("GITHUB_REPOSITORY", "")


def token() -> str:
    return os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""


def remote_url() -> str:
    return os.getenv("REELAGENT_REMOTE") or f"https://github.com/{repo()}.git"


def git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if token() and not os.getenv("REELAGENT_REMOTE"):
        basic = base64.b64encode(f"x-access-token:{token()}".encode()).decode()
        cmd += ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
    p = subprocess.run(cmd + args, cwd=str(cwd), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(safety.scrub(f"git {' '.join(args[:3])} failed: {p.stderr.strip()[-400:]}"))
    return p


class GitHub:
    """The few REST calls the agent needs. A fake stands in for it in tests (REELAGENT_FAKE_GH=folder)."""

    def __init__(self):
        self.repo, self.token = repo(), token()

    def _h(self, accept="application/vnd.github+json") -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": accept,
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "reel-agent"}

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 60)
        kw.setdefault("headers", self._h())
        return requests.request(method, url if url.startswith("http") else f"{API}/repos/{self.repo}{url}", **kw)

    def release(self) -> dict:
        r = self._req("GET", f"/releases/tags/{REELS_TAG}")
        if r.status_code == 404:
            r = self._req("POST", "/releases", json={
                "tag_name": REELS_TAG, "name": "Reels waiting to be posted", "prerelease": True,
                "body": "Full-quality reels the agent has edited and not posted yet. The agent manages these files."})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"GitHub releases: HTTP {r.status_code} {r.text[:160]}")
        return r.json()

    def assets(self) -> dict[str, dict]:
        rel = self.release()
        out, page = {}, 1
        while True:
            r = self._req("GET", f"/releases/{rel['id']}/assets", params={"per_page": 100, "page": page})
            items = r.json() if r.status_code == 200 else []
            out.update({a["name"]: a for a in items})
            if len(items) < 100:
                return out
            page += 1

    def upload(self, path: Path, name: str) -> None:
        rel = self.release()
        old = self.assets().get(name)
        if old:
            self._req("DELETE", f"/releases/assets/{old['id']}")
        with open(path, "rb") as fh:
            r = requests.post(f"https://uploads.github.com/repos/{self.repo}/releases/{rel['id']}/assets",
                              params={"name": name}, data=fh, timeout=1800,
                              headers={**self._h(), "Content-Type": "video/mp4"})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"GitHub upload: HTTP {r.status_code} {r.text[:160]}")

    def download(self, name: str, dest: Path) -> bool:
        a = self.assets().get(name)
        if not a:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._req("GET", f"/releases/assets/{a['id']}", headers=self._h("application/octet-stream"),
                       stream=True, timeout=1800) as r:
            if r.status_code != 200:
                return False
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        return dest.stat().st_size > 0

    def delete(self, name: str) -> None:
        a = self.assets().get(name)
        if a:
            self._req("DELETE", f"/releases/assets/{a['id']}")

    def alert(self, title: str, body: str) -> bool:
        """Post the alert as a comment on one issue assigned to the owner, so GitHub notifies them."""
        owner = os.getenv("GITHUB_REPOSITORY_OWNER") or self.repo.split("/")[0]
        r = self._req("GET", "/issues", params={"state": "open", "per_page": 100})
        num = None
        if r.status_code == 200:
            num = next((i["number"] for i in r.json() if i.get("title") == ALERT_ISSUE and "pull_request" not in i), None)
        if not num:
            intro = ("The reel agent posts here whenever it makes reels, posts one, runs out of clips or hits a "
                     "problem. GitHub sends these to you in its app and by email.")
            r = self._req("POST", "/issues", json={"title": ALERT_ISSUE, "body": intro, "assignees": [owner]})
            if r.status_code not in (200, 201):
                return False
            num = r.json()["number"]
        r = self._req("POST", f"/issues/{num}/comments", json={"body": f"**{title}**\n\n{body}"})
        return r.status_code in (200, 201)


class FakeGitHub(GitHub):
    """Stores 'release files' in a folder and alerts in a text file. For tests."""

    def __init__(self, folder: str):
        self.dir = Path(folder)
        (self.dir / "assets").mkdir(parents=True, exist_ok=True)

    def assets(self):
        return {p.name: {"id": p.name, "name": p.name} for p in (self.dir / "assets").iterdir()}

    def upload(self, path, name):
        shutil.copy(path, self.dir / "assets" / name)

    def download(self, name, dest):
        src = self.dir / "assets" / name
        if not src.exists():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        return True

    def delete(self, name):
        (self.dir / "assets" / name).unlink(missing_ok=True)

    def alert(self, title, body):
        with open(self.dir / "alerts.txt", "a", encoding="utf-8") as f:
            f.write(f"## {title}\n{body}\n\n")
        return True


def github() -> GitHub:
    fake = os.getenv("REELAGENT_FAKE_GH")
    return FakeGitHub(fake) if fake else GitHub()


# ── the records branch ──────────────────────────────────────────────────────

def pull_data(home: Path = HOME) -> None:
    """Check out the agent's records into `home` (must happen before the settings are read)."""
    if (home / ".git").exists():
        return
    url = remote_url()
    exists = bool(git(["ls-remote", "--heads", url, DATA_BRANCH], cwd=ROOT).stdout.strip())
    if exists:
        git(["clone", "--quiet", "--depth", "1", "--single-branch", "--branch", DATA_BRANCH, url, str(home)], cwd=ROOT)
    else:
        git(["init", "--quiet", "-b", DATA_BRANCH], cwd=home)
        git(["remote", "add", "origin", url], cwd=home)
    git(["config", "user.name", "reel-agent"], cwd=home)
    git(["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=home)
    (home / ".gitignore").write_text(GITIGNORE, encoding="utf-8")


def push_data(home: Path, message: str, squash: bool = False) -> bool:
    """Save the records. The app may have added command files meanwhile: those are kept (rebase, retry)."""
    bad = safety.leaks(home)
    if bad:      # a key reached a file despite the scrubbing: refuse to publish those files
        for rel in bad:
            f = home / rel
            if f.suffix in (".json", ".md", ".txt", ".log", ".yaml"):
                f.write_text(safety.scrub(f.read_text("utf-8", "replace")), encoding="utf-8")
        still = safety.leaks(home)
        if still:
            raise RuntimeError(f"refusing to save: a key was found in {', '.join(still[:3])}")
    git(["add", "-A"], cwd=home)
    if git(["diff", "--cached", "--quiet"], cwd=home, check=False).returncode == 0:
        return False
    git(["commit", "--quiet", "-m", message], cwd=home)
    for attempt in range(5):
        if git(["push", "--quiet", "origin", f"HEAD:{DATA_BRANCH}"], cwd=home, check=False).returncode == 0:
            break
        git(["fetch", "--quiet", "--depth", "30", "origin", DATA_BRANCH], cwd=home, check=False)
        if git(["rebase", "FETCH_HEAD"], cwd=home, check=False).returncode != 0:
            git(["rebase", "--abort"], cwd=home, check=False)
            git(["merge", "--quiet", "-X", "ours", "--no-edit", "--allow-unrelated-histories", "FETCH_HEAD"],
                cwd=home, check=False)
        time.sleep(2 + attempt)
    else:
        raise RuntimeError("could not save the agent's records to GitHub after 5 tries")
    if squash:   # once a day: keep the branch one commit deep so the repository stays small
        head = git(["rev-parse", "HEAD"], cwd=home).stdout.strip()
        new = git(["commit-tree", "HEAD^{tree}", "-m", "The agent's records (history trimmed daily)"], cwd=home).stdout.strip()
        if git(["push", "--quiet", f"--force-with-lease={DATA_BRANCH}:{head}", "origin", f"{new}:refs/heads/{DATA_BRANCH}"],
               cwd=home, check=False).returncode == 0:
            git(["reset", "--quiet", "--soft", new], cwd=home, check=False)
    return True


# ── tools on the runner ─────────────────────────────────────────────────────

def ensure_ffmpeg(log) -> bool:
    from . import media
    if media.tool_ok("ffmpeg"):
        return True
    if not os.getenv("GITHUB_ACTIONS"):
        return False
    log("installing ffmpeg on GitHub's computer (about half a minute)")
    subprocess.run("sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg >/dev/null", shell=True)
    media.tool_ok.cache_clear()
    media.has_filter.cache_clear()
    return media.tool_ok("ffmpeg")


def ensure_ai() -> None:
    """The Claude library is only needed when a Claude key is set (free Gemini needs nothing extra)."""
    if os.getenv("ANTHROPIC_API_KEY") and os.getenv("GITHUB_ACTIONS"):
        try:
            import anthropic  # noqa: F401
        except Exception:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "anthropic"], capture_output=True)


def ensure_extras(cfg, log) -> None:
    """Optional helpers for editing runs: the free offline voice. Never fatal."""
    if not os.getenv("GITHUB_ACTIONS"):
        return
    v = cfg.get("voiceover") or {}
    if v.get("mode", "auto") != "off" and v.get("engine", "piper") == "piper" and cfg.ai_available \
            and not (cfg.ig_user_id and cfg.ig_token):
        try:
            import piper  # noqa: F401
        except Exception:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "piper-tts"], capture_output=True)


# ── commands from the app ───────────────────────────────────────────────────

def _merge(base: dict, new: dict) -> dict:
    out = dict(base)
    for k, v in (new or {}).items():
        out[k] = _merge(out.get(k) or {}, v) if isinstance(v, dict) and isinstance(out.get(k, {}), dict) else v
    return out


def apply_commands(cfg, db, log) -> dict:
    """Run the command files the app left in commands/. Returns {'post_now': [ids], 'changed': bool}."""
    from .db import copy_of
    folder = HOME / "commands"
    result = {"post_now": [], "changed": False, "feedback": []}
    if not folder.is_dir():
        return result
    for f in sorted(folder.glob("*.json")):
        try:
            cmd = json.loads(f.read_text(encoding="utf-8"))
            kind, cid = cmd.get("type"), str(cmd.get("id") or "")
            row = db.get(cid) if cid else None
            if kind == "settings":
                data = {k: v for k, v in (cmd.get("data") or {}).items() if k in SETTINGS_KEYS}
                cfg.save_user_config(_merge(cfg.user_config(), data))
                result["changed"] = True
                log("⚙️ settings updated from the app: " + ", ".join(sorted(data)))
            elif kind == "post_now" and row and row["status"] in ("queued", "ready", "failed"):
                db.update(cid, status="queued", attempts=0, error=None, copy_json={**copy_of(row), "approved": True})
                result["post_now"].append(cid)
                log(f"👍 {cid} approved in the app, posting now")
            elif kind == "feedback" and row and row["status"] != "posted" and str(cmd.get("text") or "").strip():
                from . import revise
                revise.add_chat(db, cid, "claude" if cmd.get("from") == "claude" else "you", str(cmd["text"]).strip())
                db.update(cid, copy_json={**copy_of(db.get(cid)), "editing": True})
                result["feedback"].append((cid, str(cmd["text"]).strip()))
            elif kind == "approve" and row and row["status"] in ("ready", "queued"):
                db.update(cid, status="queued", copy_json={**copy_of(row), "approved": True})
                log(f"👍 {cid} approved in the app, it goes out at the next posting time")
            elif kind == "skip" and row and row["status"] != "posted":
                db.update(cid, status="rejected")
                log(f"⏭ {cid} skipped from the app")
            elif kind == "song" and row and row["status"] != "posted":
                opts = copy_of(row).get("ig_audio_options") or []
                pick = next((o for o in opts if str(o.get("audio_id")) == str(cmd.get("audio_id"))), None)
                if pick:
                    db.update(cid, copy_json={**copy_of(row), "editing": True})
                    result["feedback"].append((cid, {"song": pick}))
                    log(f"🎵 {cid}: you picked \"{pick.get('title', '')}\"")
            elif kind == "shorten" and row and row["status"] in ("failed", "ready", "queued"):
                secs = max(10, min(90, int(cmd.get("seconds") or 38)))
                db.update(cid, copy_json={**copy_of(row), "editing": True})
                result["feedback"].append((cid, {"shorten": secs}))
                log(f"✂ {cid}: you asked for a {secs}-second version")
            elif kind == "mark_posted" and row and row["status"] != "posted":
                url = str(cmd.get("url") or "").strip()
                db.update(cid, status="posted", posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          ig_url=url if url.startswith("https://www.instagram.com/") else "", ig_status="by_owner",
                          copy_json={**copy_of(row), "posted_by_owner": True})
                log(f"📲 {cid} posted by you from the Instagram app")
            elif kind == "profile":
                from . import profile
                profile.flag(cfg).write_text("1", encoding="utf-8")
                log("🪪 the app asked for a fresh profile check")
            elif kind == "retry" and row and row["status"] == "failed":
                db.update(cid, status="queued", attempts=0, error=None)
            elif kind == "caption" and row and row["status"] != "posted":
                copy = copy_of(row)
                if isinstance(cmd.get("caption"), str):
                    copy["instagram_caption"] = cmd["caption"].strip()[:2000]
                if isinstance(cmd.get("hashtags"), list):
                    from .captions import _clean_hashtags
                    copy["hashtags"] = _clean_hashtags(cmd["hashtags"], [])
                db.update(cid, copy_json=copy)
                log(f"✏️ caption of {cid} edited in the app")
            elif kind in ("post_now", "approve", "skip", "retry", "caption", "feedback"):
                log(f"({kind} from the app ignored: reel {cid or '?'} is not in a state where that applies)")
            elif kind not in ("wake", None):
                log(f"(ignored a command the agent doesn't know: {kind})")
        except Exception as e:
            log(f"! could not apply {f.name}: {e}")
        f.unlink(missing_ok=True)
    return result


# ── clips uploaded from the app ─────────────────────────────────────────────

def inbox_refs() -> list[str]:
    out = git(["ls-remote", "--heads", remote_url(), f"{INBOX_PREFIX}*"], cwd=ROOT, check=False).stdout
    return sorted(l.split("refs/heads/", 1)[1].strip() for l in out.splitlines() if "refs/heads/" in l)


GROUP_RE = re.compile(r"^inbox/g-(?P<gid>[0-9a-z-]+?)-(?P<k>\d+)of(?P<n>\d+)$")


def batches(refs: list[str], hist: dict, log, wait_hours: float = 3.0) -> list[list[str]]:
    """Each upload on its own, except clips the owner added together as one session ("edit them together"):
    those come as one list, in order, once every part has arrived (or after a few hours with what came)."""
    out, groups = [], {}
    for r in refs:
        m = GROUP_RE.match(r)
        if m:
            groups.setdefault(m["gid"], {})[int(m["k"])] = (r, int(m["n"]))
        else:
            out.append([r])
    for gid, parts in groups.items():
        n = max(v[1] for v in parts.values())
        seen = hist.setdefault(f"group:{gid}", {"first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(seen["first_seen"])).total_seconds() / 3600
        if len(parts) >= n or age >= wait_hours:
            out.append([parts[k][0] for k in sorted(parts)])
        else:
            log(f"⏳ waiting for the rest of a session you added together ({len(parts)} of {n} clips arrived)")
    return out


def join_clips(paths: list[Path], out: Path, log) -> Path:
    """Put the clips of one session end to end. Same camera settings: joined without re-encoding (no quality
    lost). Otherwise each is scaled to the first one's size and re-encoded once."""
    from . import media
    infos = [media.probe(p) for p in paths]

    def sig(p):
        rc, so, _ = media.capture(["ffprobe", "-v", "error", "-show_entries",
                                   "stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt,sample_rate,channels,"
                                   "color_transfer", "-of", "compact", str(p)])
        return so.strip() if rc == 0 else str(p)
    lst = out.parent / f"{out.stem}_list.txt"
    if len({sig(p) for p in paths}) == 1:
        lst.write_text("".join(f"file '{p.resolve()}'\n" for p in paths), encoding="utf-8")
        try:
            media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0",
                       "-c", "copy", "-movflags", "+faststart", str(out)])
            got = media.probe(out).duration
            want = sum(i.duration for i in infos)
            if abs(got - want) <= max(2.0, 0.02 * want):
                log(f"  🔗 joined {len(paths)} clips ({want / 60:.1f} min) without re-encoding")
                return out
        except Exception as e:
            log(f"  (joining without re-encoding didn't work: {str(e)[:120]}; re-encoding)")
    w, h = infos[0].width, infos[0].height
    if max(w, h) > 1920:                                     # keep it fast: 1080p is plenty for a phone screen
        k = 1920 / max(w, h)
        w, h = int(w * k) // 2 * 2, int(h * k) // 2 * 2
    cmd, chains, ins = ["ffmpeg", "-y", "-v", "error"], [], 0
    for p, info in zip(paths, infos):
        cmd += ["-i", str(p)]
        tone = media.tonemap_chain(info)
        chains.append(f"[{ins}:v]{tone + ',' if tone else ''}scale={w}:{h}:force_original_aspect_ratio=decrease,"
                      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{ins}]")
        if info.has_audio:
            chains.append(f"[{ins}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{ins}]")
        else:
            chains.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{info.duration:.3f}[a{ins}]")
        ins += 1
    chains.append("".join(f"[v{i}][a{i}]" for i in range(ins)) + f"concat=n={ins}:v=1:a=1[v][a]")
    cmd += media.graph_args(";".join(chains), out.parent, f"{out.stem}_join.txt")
    cmd += ["-map", "[v]", "-map", "[a]", *media.encode_args(17, "veryfast"), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    media.run(cmd)
    log(f"  🔗 joined {len(paths)} clips ({sum(i.duration for i in infos) / 60:.1f} min), re-encoded to {w}x{h}")
    return out


def fetch_upload(ref: str, work: Path) -> tuple[Path, dict]:
    """Download one uploaded clip (a branch of 5 MB parts) and put it back together."""
    local = f"refs/reelagent/{ref}"
    git(["fetch", "--quiet", "--depth", "1", remote_url(), f"+refs/heads/{ref}:{local}"], cwd=ROOT)
    parts_dir = work / "parts" / ref.replace("/", "_")
    parts_dir.mkdir(parents=True, exist_ok=True)
    tar = parts_dir / "upload.tar"
    git(["archive", "--format=tar", "-o", str(tar), local], cwd=ROOT)
    with tarfile.open(tar) as t:
        members = [m for m in t.getmembers() if m.isfile() and re.fullmatch(r"(meta\.json|part-\d{5})", m.name)]
        try:
            t.extractall(parts_dir, members=members, filter="data")
        except TypeError:                      # Python before 3.12
            t.extractall(parts_dir, members=members)
    tar.unlink()
    git(["update-ref", "-d", local], cwd=ROOT, check=False)
    meta = json.loads((parts_dir / "meta.json").read_text(encoding="utf-8"))
    name = re.sub(r"[^\w.-]+", "_", Path(str(meta.get("name") or "clip.mp4")).name).strip("._") or "clip.mp4"
    if Path(name).suffix.lower() not in VIDEO_EXT:
        name += ".mp4"
    game = str(meta.get("game") or "auto").lower()
    dest = work / "inbox" / (game if game not in ("auto", "") else "") / f"{ref.split('/')[-1]}_{name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts = sorted(parts_dir.glob("part-*"))
    with open(dest, "wb") as out:
        for p in parts:
            with open(p, "rb") as fh:
                shutil.copyfileobj(fh, out, 1 << 20)
    shutil.rmtree(parts_dir, ignore_errors=True)
    if meta.get("size") and dest.stat().st_size != int(meta["size"]):
        raise ValueError(f"the upload is incomplete ({dest.stat().st_size} of {meta['size']} bytes): add the clip again")
    return dest, meta


def hold_unapproved(cfg, db, log) -> None:
    """With approval on, every waiting reel the owner hasn't approved moves to 'waiting for your OK'."""
    from . import autopilot
    if not (cfg.get("review") or {}).get("require_approval", True):
        return
    held = [r["id"] for r in db.clips("queued") if not autopilot.approved(cfg, r)]
    for cid in held:
        db.update(cid, status="ready")
    if held:
        log(f"✋ {len(held)} reel(s) now wait for your approval: {', '.join(held)}")


def store_reel(cfg, db, gh: GitHub, cid: str, log) -> None:
    """A small preview for the app, and the full-quality reel in the release files."""
    folder = cfg.output / "clips" / cid
    reel = folder / "reel.mp4"
    try:
        from . import media
        media.run(["ffmpeg", "-y", "-v", "error", "-i", str(reel), "-vf", "scale=540:-2", "-r", "30",
                   *media.encode_args(31, "veryfast"), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
                   "-movflags", "+faststart", str(folder / "preview.mp4")])
    except Exception as e:
        log(f"  (no preview for {cid}: {str(e)[:120]})")
    for i in range(3):
        try:
            gh.upload(reel, f"{cid}.mp4")
            return
        except Exception as e:
            if i == 2:
                db.update(cid, status="failed", error=f"could not store the reel: {e}")
                log(f"! {cid}: could not store the reel ({e})")
            time.sleep(5)


def answer_notes(cfg, db, gh: GitHub, notes: list, log, events: list) -> None:
    """The owner's chat notes: re-edit each reel as asked and reply."""
    from . import revise
    from .db import copy_of, render_of
    if not notes:
        return
    ensure_ffmpeg(log)
    work = Path(tempfile.mkdtemp(prefix="reelagent-edit-"))
    sources: dict[str, Path | None] = {}
    for cid, text in notes:
        row = db.get(cid)
        if not row:
            continue
        name = render_of(row).get("source_asset") or ""
        if name and name not in sources:
            dest = work / name
            sources[name] = dest if gh.download(name, dest) else None
        src = sources.get(name) if name else (Path(row["source"]) if Path(row["source"]).exists() else None)
        before = render_of(row).get("version", 1)
        if isinstance(text, dict) and "shorten" in text:
            reply = revise.shorten(cfg, db, cid, int(text["shorten"]), src)
        elif isinstance(text, dict):
            reply = revise.use_song(cfg, db, cid, text["song"], src)
        else:
            log(f"💬 {cid}: \"{text[:120]}\"")
            reply = revise.handle(cfg, db, cid, text, src)
        row = db.get(cid)
        copy = copy_of(row)
        copy.pop("editing", None)
        db.update(cid, copy_json=copy)
        if render_of(row).get("version", 1) != before:
            store_reel(cfg, db, gh, cid, log)
        log(f"   ↳ {reply}")
        events.append((f"✏️ {cid} re-edited, waiting for your OK" if render_of(row).get("version", 1) != before
                       else f"💬 Reply about {cid}", reply))
    shutil.rmtree(work, ignore_errors=True)


def process_inbox(cfg, db, gh: GitHub, log, events: list) -> None:
    from . import media, pipeline
    refs = inbox_refs()
    hist_file = cfg.state_dir / "uploads.json"
    try:
        hist = json.loads(hist_file.read_text(encoding="utf-8"))
    except Exception:
        hist = {}
    if not refs:
        return
    if not ensure_ffmpeg(log):
        log("! ffmpeg is not available here, so the clips can't be edited yet")
        return
    ensure_extras(cfg, log)
    work = Path(tempfile.mkdtemp(prefix="reelagent-"))
    cfg.inbox = work / "inbox"
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    from . import request as rq
    for batch in batches(refs, hist, log):
        ref = batch[0] if len(batch) == 1 else batch[0].rsplit("-", 1)[0]       # a session: inbox/g-<id>
        h = hist.setdefault(ref, {"name": ref, "attempts": 0})
        h["attempts"] += 1
        h["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        made, note, done = [], "", True
        try:
            if len(batch) == 1:
                path, meta = fetch_upload(ref, work)
                h["name"] = str(meta.get("name") or ref)[:80]
            else:
                got = [fetch_upload(r, work) for r in batch]
                meta = got[0][1]
                names = [str(m.get("name") or "clip") for _, m in got]
                h["name"] = (f"{len(got)} clips joined: " + " + ".join(names))[:80]
                if (meta.get("of") or len(got)) > len(got):
                    log(f"  ! only {len(got)} of {meta.get('of')} clips of the session arrived; joining those")
                path = join_clips([p for p, _ in got], got[0][0].with_name(f"{ref.split('/')[-1]}_session.mp4"), log)
                for p, _ in got:
                    p.unlink(missing_ok=True)
            game = str(meta.get("game") or "auto").lower()
            plan = rq.clean(meta.get("plan"))
            h["plan"] = rq.label(plan)
            made = pipeline.process_file(cfg, db, path, game if game in cfg["games"] and game != "default" else None,
                                         plan=plan)
            src = db.conn.execute("SELECT * FROM sources WHERE path=?", (str(path.resolve()),)).fetchone()
            if src and src["status"] == "skipped":
                note = src["note"] or "skipped"
            if made:          # keep the original so the chat can re-edit these reels later
                src_name = f"src-{ref.split('/')[-1]}{path.suffix.lower()}"
                try:
                    gh.upload(path, src_name)
                except Exception as e:
                    src_name = ""
                    log(f"  (couldn't keep the original for re-edits: {str(e)[:120]})")
            for cid in made:
                if src_name:
                    from .db import render_of
                    db.update(cid, render_json={**render_of(db.get(cid)), "source_asset": src_name})
                store_reel(cfg, db, gh, cid, log)
            path.unlink(missing_ok=True)
            if made:
                from .db import copy_of
                hooks = [copy_of(db.get(c)).get("hook_text", "") for c in made]
                waiting = (cfg.get("review") or {}).get("require_approval", True)
                events.append((f"🎬 {len(made)} reel{'s' if len(made) != 1 else ''} made from {h['name']}"
                               + (", waiting for your OK" if waiting else ""),
                               "\n".join(f"- {c}: \"{x}\"" for c, x in zip(made, hooks))
                               + ("\n\nOpen the app, watch them, and tap Approve on the ones you want posted." if waiting else "")))
            h.update(status="done" if made else "nothing", reels=made,
                     note=note or ("" if made else "no moment was strong enough to post"))
        except Exception as e:
            note = safety.scrub(str(e))[:300]
            log(f"❌ could not edit {h.get('name', ref)}: {note}")
            done = h["attempts"] >= 2 or isinstance(e, ValueError)
            h.update(status="failed" if done else "retrying", note=note)
            if done:
                events.append((f"❌ Could not edit {h.get('name', ref)}", note))
        if done:
            for r in batch:
                git(["push", "--quiet", remote_url(), "--delete", f"refs/heads/{r}"], cwd=ROOT, check=False)
    keep = sorted(hist.items(), key=lambda kv: kv[1].get("at", ""))[-30:]
    hist_file.write_text(json.dumps(dict(keep), indent=1, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)


# ── posting ─────────────────────────────────────────────────────────────────

def due(cfg, db, now: datetime | None = None) -> bool:
    """A posting time has come (or passed today without its reel). Posts up to 7 minutes early so a
    run every 15 minutes lands close to the time instead of always after it."""
    from . import phone, reach
    n = phone._now(cfg, now)
    if not (phone.in_active_hours(cfg, n) and phone.under_daily_max(cfg, db, n) and phone.gap_ok(cfg, db, n)):
        return False
    reached = [s for s in reach.post_times(cfg) if phone._t(s) <= (n + EARLY).time() or (n + EARLY).date() > n.date()]
    return len(reached) > len(phone._posted_today(cfg, db, n))


def schedule(cfg, db, count: int) -> list[str]:
    """When the next `count` queued reels are expected to go out (local ISO times)."""
    from . import phone, reach
    now = phone._now(cfg)
    slots, day = [], now.date()
    posted_today = len(phone._posted_today(cfg, db, now))
    times = sorted(reach.post_times(cfg), key=phone._t)[: int(cfg["schedule"]["max_per_day"])] or ["18:00"]
    d = 0
    while len(slots) < count and d < 30:
        todays = [datetime.combine(day + timedelta(days=d), phone._t(s), tzinfo=now.tzinfo) for s in times]
        if d == 0:
            todays = todays[posted_today:]              # today's first slots already have their reels
            todays = [max(t, now) for t in todays]
        slots += todays
        d += 1
    return [t.isoformat(timespec="minutes") for t in slots[:count]]


def ensure_reel(cfg, db, gh: GitHub, row) -> bool:
    path = cfg.output / "clips" / row["id"] / "reel.mp4"
    if not path.exists() and not gh.download(f"{row['id']}.mp4", path):
        return False
    if row["file"] != str(path):
        db.update(row["id"], file=str(path))
    return True


def remake_missing(cfg, db, gh: GitHub, row, log) -> bool:
    """The edited reel file is gone but the original clip is stored: edit it again, keeping the approval."""
    from . import revise
    from .db import copy_of, render_of
    asset = render_of(row).get("source_asset")
    if not asset:
        return False
    src = cfg.output / "sources" / asset
    if not src.exists() and not gh.download(asset, src):
        return False
    log(f"🔁 {row['id']}: the edited file was gone, editing it again from your original clip")
    approved = copy_of(row).get("approved")
    revise.remake(cfg, db, row["id"], {}, src)
    copy = copy_of(db.get(row["id"]))
    copy["approved"] = approved
    db.update(row["id"], copy_json=copy, status=row["status"])
    store_reel(cfg, db, gh, row["id"], log)
    return ensure_reel(cfg, db, gh, db.get(row["id"]))


def post_one(cfg, db, gh: GitHub, row, log, events: list) -> bool:
    from . import autopilot
    from .db import copy_of
    if not ensure_reel(cfg, db, gh, row) and not remake_missing(cfg, db, gh, row, log):
        db.update(row["id"], status="failed", error="the edited reel file is missing; add the clip again")
        return False
    ok = autopilot.post(cfg, db, db.get(row["id"]))
    row = db.get(row["id"])
    if ok:
        events.append((f"✅ Posted: \"{copy_of(row).get('hook_text', '')}\"", row["ig_url"] or ""))
    elif row["status"] == "failed":
        events.append((f"❌ Could not post {row['id']}", safety.scrub(row["error"] or "")))
    return ok


def tidy(cfg, db, gh: GitHub) -> None:
    """Drop stored videos of reels that are posted, skipped or failed for good; keep covers of the last 60."""
    try:
        assets = gh.assets()
    except Exception:
        assets = {}
    rows = db.clips()
    # 'ready' = waiting for approval; failed reels stay until you skip them (you may retry or shorten them)
    alive = {r["id"] for r in rows if r["status"] in ("queued", "ready", "failed")}
    from .db import render_of
    needed = {render_of(r).get("source_asset") for r in rows if r["status"] in ("queued", "ready", "failed")}
    for name in assets:
        if name.startswith("src-"):
            if name not in needed:
                gh.delete(name)              # all its reels are posted or skipped: no more re-edits
            continue
        if name.endswith(".mp4") and name[:-4] not in alive:
            gh.delete(name)
    done = [r for r in rows if r["status"] in ("posted", "rejected", "skipped")]
    for r in done:
        (cfg.output / "clips" / r["id"] / "preview.mp4").unlink(missing_ok=True)
    for r in done[:-60]:
        shutil.rmtree(cfg.output / "clips" / r["id"], ignore_errors=True)


# ── what the app shows ──────────────────────────────────────────────────────

def _clip(cfg, row, insights: dict, eta: str | None = None) -> dict:
    from . import request as rq
    from .db import copy_of, render_of
    from .formats import FORMATS
    c, r = copy_of(row), render_of(row)
    base = f"output/clips/{row['id']}"
    folder = cfg.output / "clips" / row["id"]
    game = cfg.game_profile(row["game"])
    d = {"id": row["id"], "game": row["game"], "game_name": game.get("name", row["game"]), "status": row["status"],
         "hook": c.get("hook_text", ""), "caption": c.get("instagram_caption", ""), "hashtags": c.get("hashtags", []),
         "quality": row["quality"], "hook_score": c.get("hook_score"),
         "style": r.get("style", "classic"), "style_name": FORMATS.get(r.get("style", "classic"), {}).get("name", ""),
         "style_why": c.get("style_why", ""), "look": (r.get("fx") or {}).get("look"),
         "seconds": r.get("duration"), "created": row["created_at"],
         "cover": f"{base}/cover.jpg" if (folder / "cover.jpg").exists() else None,
         "preview": f"{base}/preview.mp4" if (folder / "preview.mp4").exists() else None,
         "ig_sound": ({k: (c.get("ig_audio") or c.get("ig_audio_plan") or {}).get(k, "") for k in ("title", "artist")}
                      if (c.get("ig_audio") or c.get("ig_audio_plan")) else None),
         "sound": (c.get("ig_audio") or c.get("ig_audio_plan") or {}).get("title") or
                  ("a beat made for it" if str(r.get("music") or "").startswith("beat:") else "your music" if r.get("music") else
                                                             "voiceover" if r.get("voice") else None),
         "what": c.get("what_happens", ""),
         "song_options": [{k: o.get(k, "") for k in ("audio_id", "title", "artist", "link")}
                          for o in (c.get("ig_audio_options") or [])[:3]],
         "song_ideas": c.get("song_ideas") or [],
         "music_baked": bool(r.get("music")), "by_owner": bool(c.get("posted_by_owner")),
         "asked": rq.label(c["owner_plan"]) if c.get("owner_plan") else "",
         "too_long": "[too_long]" in str(row["error"] or ""),
         "download": f"https://github.com/{repo()}/releases/download/{REELS_TAG}/{row['id']}.mp4" if repo() else None}
    if r.get("format") == "full" or r.get("style") == "highlight":
        from .formats import ON_REQUEST
        d["style_name"] = ON_REQUEST["full" if r.get("format") == "full" else "highlight"]["name"]
    if eta:
        d["eta"] = eta
    if row["status"] == "posted":
        d.update(posted_at=row["posted_at"], url=row["ig_url"], trial=bool(c.get("trial")))
        i = insights.get(row["id"])
        if i:
            d["stats"] = {k: i[k] for k in ("views", "reach", "likes", "comments", "shares", "saved") if i[k] is not None}
    if row["error"]:
        d["error"] = str(row["error"]).replace("[too_long] ", "")
    d["chat"] = (c.get("chat") or [])[-20:]
    d["editing"] = bool(c.get("editing"))
    d["approved"] = bool(c.get("approved"))
    d["version"] = int(r.get("version", 1))
    d["can_reedit"] = bool(r.get("source_asset")) or Path(row["source"] or "/nonexistent").exists()
    return d


def build_status(cfg, db, account: dict | None, note: str = "") -> dict:
    from . import ai, autopilot, phone, profile, reach, scout, trends
    from .formats import FORMATS, allowed
    insights = {r["clip_id"]: r for r in db.conn.execute("SELECT * FROM insights").fetchall()}
    rows = db.clips()
    queue = autopilot.queue_order(cfg, db)
    etas = schedule(cfg, db, len(queue))
    posted = [r for r in rows if r["status"] == "posted"][-40:][::-1]
    problems = [r for r in rows if r["status"] == "failed"][-10:][::-1]
    try:
        uploads = [u for u in json.loads((cfg.state_dir / "uploads.json").read_text(encoding="utf-8")).values()
                   if u.get("status")][-8:][::-1]
    except Exception:
        uploads = []
    try:
        log_tail = (cfg.state_dir / "agent.log").read_text(encoding="utf-8").splitlines()[-120:]
    except Exception:
        log_tail = []
    brief, sc = trends.load(cfg), scout.load(cfg)
    learned = {}
    try:
        learned = json.loads((cfg.state_dir / "learnings.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    times = reach.post_times(cfg)
    try:
        smart = json.loads((cfg.state_dir / "smart_times.json").read_text(encoding="utf-8")).get("times")
    except Exception:
        smart = None
    if not cfg["schedule"].get("auto_times", True):
        source = "the times you set"
    elif smart and times == smart:
        source = "when your followers are online"
    elif times and times == scout.post_times(cfg):
        source = "when top posts in your niche go live"
    else:
        source = "the starting times (the agent tunes these as it learns)"
    user = cfg.user_config()
    sv = [i for i in insights.values()]
    status = {
        "v": 1,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
        "agent": {
            "paused": autopilot.paused(cfg), "connected": bool(cfg.ig_user_id and cfg.ig_token),
            "username": (account or {}).get("username") or user.get("ig_username") or "",
            "followers": (account or {}).get("followers_count"),
            "ai": ai.provider(cfg), "youtube": bool(os.getenv("YOUTUBE_API_KEY")),
            "handle": cfg.get("handle", ""), "timezone": cfg.get("timezone"),
            "version": (os.getenv("GITHUB_SHA") or "")[:7], "run": os.getenv("GITHUB_RUN_ID"),
        },
        "schedule": {"times": times, "source": source, "max_per_day": int(cfg["schedule"]["max_per_day"]),
                     "posted_today": len(phone._posted_today(cfg, db)),
                     "active_hours": (cfg.get("phone") or {}).get("active_hours"),
                     "now_local": phone._now(cfg).isoformat(timespec="minutes")},
        "settings": {
            "handle": cfg.get("handle", ""), "timezone": cfg.get("timezone"),
            "max_per_day": int(cfg["schedule"]["max_per_day"]), "auto_times": bool(cfg["schedule"].get("auto_times", True)),
            "post_times": [str(t) for t in cfg["schedule"]["post_times"]],
            "per_video": int(cfg["clips"]["per_video"]), "length": float(cfg["clips"]["length"]),
            "style": cfg["clips"].get("style", "auto"), "styles": allowed(cfg),
            "creativity": float(cfg["clips"].get("creativity", 0.2)), "end_cards": bool(cfg["clips"].get("end_cards", True)),
            "look": (cfg["clips"].get("fx") or {}).get("look", "auto"),
            "require_approval": bool((cfg.get("review") or {}).get("require_approval", True)),
            "reply_to_comments": bool((cfg.get("engagement") or {}).get("reply_to_comments", True)),
            "trending_audio": bool((cfg.get("instagram") or {}).get("trending_audio", True)),
            "beats": bool((cfg.get("music") or {}).get("generate", False)),
            "long_refused": reach.flagged(cfg, "long_refused"),
            "benchmark_pages": (cfg.get("trends") or {}).get("benchmark_pages") or [],
            "games": {k: v.get("name", k) for k, v in cfg["games"].items() if k != "default"},
            "all_styles": {k: v["name"] for k, v in FORMATS.items()},
        },
        "queue": [_clip(cfg, r, insights, etas[i] if i < len(etas) else None) for i, r in enumerate(queue)],
        "awaiting": [_clip(cfg, r, insights) for r in sorted(db.clips("ready"), key=lambda r: r["created_at"], reverse=True)],
        "approval": bool((cfg.get("review") or {}).get("require_approval", True)),
        "posted": [_clip(cfg, r, insights) for r in posted],
        "problems": [_clip(cfg, r, insights) for r in problems],
        "uploads": uploads,
        "totals": {"posted": sum(1 for r in rows if r["status"] == "posted"),
                   "views": sum((i["views"] or 0) for i in sv), "likes": sum((i["likes"] or 0) for i in sv),
                   "shares": sum((i["shares"] or 0) for i in sv)},
        "learned": {
            "trends": {"date": brief.get("date"), "summary": brief.get("summary", ""),
                       "reel_types": (brief.get("reel_types") or {}).get("recommended") or [],
                       "new_ideas": (brief.get("reel_types") or {}).get("new_ideas") or [],
                       "algorithm": brief.get("algorithm_updates") or []} if brief else None,
            "instagram": {"updated": sc.get("updated"), "notes": sc.get("notes") or [],
                          "games": {g: {"posts": d.get("posts"), "tags": [h["tag"] for h in d.get("hashtags", [])[:8]],
                                        "kinds": d.get("formats", [])[:6], "playbook": d.get("playbook"),
                                        "openers": [c["line"] for c in d.get("top_captions", [])[:3]]}
                                    for g, d in (sc.get("games") or {}).items()}} if sc else None,
            "page": {"reels": learned.get("n"), "confidence": learned.get("confidence"),
                     "by_kind": learned.get("style_weights") or {}} if learned else None,
        },
        "profile": profile.for_status(cfg),
        "log": log_tail,
    }
    return safety.scrub_obj(status)


# ── one run ─────────────────────────────────────────────────────────────────

def run(cfg_path=None) -> int:
    """Everything one visit does. Always tries to save the records, even after an error."""
    from . import autopilot, instagram, pipeline
    from .config import Config, load_env
    from .db import DB
    pull_data(HOME)
    load_env()
    ensure_ai()
    cfg = Config(cfg_path)
    db = DB(cfg.state_dir / "agent.db")
    gh = github()
    events: list[tuple[str, str]] = []
    note, code = "", 0

    def log(msg: str) -> None:
        pipeline.log(msg, cfg)

    try:
        cmds = apply_commands(cfg, db, log)
        if cmds["changed"]:
            cfg = Config(cfg_path)
        hold_unapproved(cfg, db, log)
        connected = bool(cfg.ig_user_id and cfg.ig_token)
        if cfg.get("handle") in (None, "", "@yourpage"):       # no watermark chosen: use the Instagram name, or none
            name = cfg.user_config().get("ig_username") or ""
            try:
                name = name or json.loads((cfg.state_dir / "account.json").read_text(encoding="utf-8")).get("username", "")
            except Exception:
                pass
            cfg.data["handle"] = f"@{name.lstrip('@')}" if name else ""

        autopilot.trends_job(cfg, db, pipeline.log)                 # fresh trends before any editing
        answer_notes(cfg, db, gh, cmds["feedback"], log, events)    # the owner is waiting on these: first
        process_inbox(cfg, db, gh, log, events)

        if connected:
            for cid in cmds["post_now"]:
                row = db.get(cid)
                if row and row["status"] == "queued":
                    post_one(cfg, db, gh, row, log, events)
            if not autopilot.paused(cfg) and due(cfg, db):
                row = autopilot.pick_next(cfg, db)
                if row:
                    post_one(cfg, db, gh, row, log, events)
                else:
                    flag = cfg.state_dir / "empty_alert.txt"
                    today = datetime.now().strftime("%Y-%m-%d")
                    waiting = len(db.clips("ready"))
                    if not flag.exists() or flag.read_text().strip() != today:
                        flag.write_text(today)
                        if waiting:
                            log(f"✋ a posting time has come; {waiting} reel(s) wait for your approval")
                            events.append(("✋ Reels waiting for your OK",
                                           f"A posting time has come and {waiting} reel{'s' if waiting != 1 else ''} "
                                           "wait for your approval. Open the app to approve or change them."))
                        else:
                            log("📭 a posting time has come but the queue is empty")
                            events.append(("📭 Nothing to post", "A posting time has come and the queue is empty. "
                                                                "Add gameplay in the app and the agent will cut new reels."))
        elif cmds["post_now"]:
            log("! can't post yet: Instagram isn't connected (Settings in the app)")

        # the quieter jobs: comments, Instagram scout, stats, weekly times, monthly profile check
        if connected and autopilot._every(db, "scout", float((cfg.get("trends") or {}).get("scout_every_hours", 48)) * 60):
            ensure_ffmpeg(log)                                       # the scout watches a few top reels
        autopilot.tick(cfg, db, due=lambda *_: False, process_inbox=False)
        tidy(cfg, db, gh)
    except Exception:
        code = 1
        note = safety.scrub(traceback.format_exc()[-1200:])
        log("! the run hit an error:\n" + note)
        events.append(("❌ The agent hit an error", f"```\n{note[-700:]}\n```"))

    account = None
    try:
        if cfg.ig_user_id and cfg.ig_token:
            cache = cfg.state_dir / "account.json"
            if cache.exists() and time.time() - cache.stat().st_mtime < 3600:
                account = json.loads(cache.read_text(encoding="utf-8"))
            else:
                account = instagram.from_config(cfg).account()
                cache.write_text(json.dumps(account), encoding="utf-8")
    except Exception as e:
        log(f"! could not read the Instagram account: {e}")
    try:
        status = build_status(cfg, db, account, note)
        (HOME / "status.json").write_text(json.dumps(status, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:
        code = 1
        pipeline.log("! could not write status.json:\n" + safety.scrub(traceback.format_exc()[-800:]), cfg)
    squash = autopilot._every(db, "squash", 24 * 60)
    if squash:
        db.job_done("squash")
    try:
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    db.conn.close()
    saved = push_data(HOME, f"agent run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", squash=squash)
    for title, body in events:
        try:
            gh.alert(safety.scrub(title), safety.scrub(body))
        except Exception:
            pass
        topic = os.getenv("NTFY_TOPIC")
        if topic:
            try:
                requests.post(f"https://ntfy.sh/{topic}", data=safety.scrub(body).encode(), timeout=20,
                              headers={"Title": safety.scrub(title).encode("ascii", "ignore").decode(), "Tags": "clapper"})
            except Exception:
                pass
    print(f"run finished: {'records saved' if saved else 'nothing changed'}, {len(events)} alert(s)")
    return code
