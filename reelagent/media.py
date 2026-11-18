"""Thin wrappers around ffmpeg / ffprobe that work on a PC, a server and an iPhone (a-Shell).

a-Shell runs ffmpeg as a built-in command, so Python's subprocess may not be able to start it.
Every call here therefore goes through `_exec`, which tries subprocess first and falls back to
os.system with output written to files. Nothing relies on reading binary data from a pipe.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy ships with a-Shell; tiny fallback keeps things running
    np = None

ASSETS = Path(__file__).resolve().parent / "assets"
FONTS = ASSETS / "fonts"


class FFmpegError(RuntimeError):
    pass


from .config import is_ios  # noqa: E402,F401  (re-exported)


# ── running commands ────────────────────────────────────────────────────────

_MODE = {"m": os.environ.get("REELAGENT_EXEC", "auto")}   # auto | subprocess | system


def _system(cmd: list[str], cwd: Path | None, out_file: Path | None) -> tuple[int, str]:
    """os.system fallback: stdout/stderr go to files (a-Shell's shell understands > and 2>)."""
    with tempfile.TemporaryDirectory() as td:
        err = Path(td) / "stderr.txt"
        line = shlex.join(cmd) + (f" > {shlex.quote(str(out_file))}" if out_file else "") \
            + f" 2> {shlex.quote(str(err))}"
        old = os.getcwd()
        try:
            if cwd:
                os.chdir(cwd)
            rc = os.system(line)
        finally:
            os.chdir(old)
        if rc and hasattr(os, "waitstatus_to_exitcode"):
            try:
                rc = os.waitstatus_to_exitcode(rc)
            except ValueError:
                pass
        text = err.read_text("utf-8", "replace") if err.exists() else ""
    return rc, text


def _exec(cmd: list[str], cwd: Path | None = None, out_file: Path | None = None) -> tuple[int, str]:
    """Run a command. Returns (returncode, stderr text). stdout goes to out_file if given."""
    if _MODE["m"] != "system":
        try:
            if out_file:
                with open(out_file, "wb") as fh:
                    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, stdout=fh, stderr=subprocess.PIPE)
            else:
                proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            _MODE["m"] = "subprocess"
            return proc.returncode, proc.stderr.decode("utf-8", "replace")
        except (OSError, NotImplementedError, RuntimeError) as e:
            if _MODE["m"] == "subprocess" and not isinstance(e, FileNotFoundError):
                raise
            _MODE["m"] = "system"           # iPhone: ffmpeg is a shell built-in, not a file on PATH
    return _system(cmd, cwd, out_file)


def run(cmd: list[str], cwd: Path | None = None, out_file: Path | None = None) -> str:
    """Run ffmpeg/ffprobe, raise FFmpegError with the tail of stderr on failure. Returns stderr."""
    rc, err = _exec(cmd, cwd, out_file)
    if rc != 0:
        raise FFmpegError(f"Command failed: {' '.join(cmd[:6])} ...\n{err[-2500:]}")
    return err


def capture(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run and return (rc, stdout text, stderr text) via a temp file."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "stdout.txt"
        rc, err = _exec(cmd, cwd, out)
        text = out.read_text("utf-8", "replace") if out.exists() else ""
    return rc, text, err


@lru_cache(maxsize=None)
def tool_ok(name: str) -> bool:
    if shutil.which(name):
        return True
    try:
        rc, out, err = capture([name, "-hide_banner", "-version"])
    except Exception:
        return False
    return rc == 0 and ("version" in (out + err).lower())


def require_ffmpeg() -> None:
    if not tool_ok("ffmpeg"):
        raise FFmpegError(
            "ffmpeg not found. Install FFmpeg first:\n"
            "  iPhone (a-Shell): pkg install ffmpeg\n"
            "  Windows: winget install Gyan.FFmpeg\n"
            "  macOS:   brew install ffmpeg\n"
            "  Linux:   sudo apt install ffmpeg"
        )


@lru_cache(maxsize=None)
def ffmpeg_major() -> int:
    rc, out, err = capture(["ffmpeg", "-hide_banner", "-version"])
    m = re.search(r"ffmpeg version n?(\d+)", out + err)
    return int(m.group(1)) if m else 6


def graph_args(graph: str, workdir: Path, name: str = "graph.txt") -> list[str]:
    """Pass a filter graph through a file so no shell ever has to quote it."""
    f = workdir / name
    f.write_text(graph, encoding="utf-8")
    if ffmpeg_major() >= 7:
        return ["-/filter_complex", f.name]
    return ["-filter_complex_script", f.name]


@lru_cache(maxsize=None)
def video_encoder(preferred: str = "auto") -> str:
    """libx264 where it exists, Apple's hardware encoder on iPhone/Mac, else whatever H.264 there is."""
    order = ["libx264", "h264_videotoolbox", "libopenh264", "h264_mediacodec", "mpeg4"]
    if preferred and preferred != "auto":
        order.insert(0, preferred)
    with tempfile.TemporaryDirectory() as td:
        for enc in order:
            rc, _ = _exec(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=128x128:d=0.2",
                           "-frames:v", "3", "-c:v", enc, "-pix_fmt", "yuv420p", "t.mp4"], cwd=Path(td))
            if rc == 0:
                return enc
    return "libx264"


def encode_args(crf: int = 20, preset: str = "veryfast", encoder: str = "auto") -> list[str]:
    enc = video_encoder(encoder)
    tags = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
    return _enc(enc, crf, preset) + tags


def _enc(enc: str, crf: int, preset: str) -> list[str]:
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-profile:v", "high"]
    if enc == "h264_videotoolbox":     # hardware: fast and cool on iPhone; bitrate-driven
        return ["-c:v", "h264_videotoolbox", "-b:v", "9M", "-maxrate", "12M", "-profile:v", "high",
                "-allow_sw", "1"]
    if enc == "libopenh264":
        return ["-c:v", "libopenh264", "-b:v", "9M"]
    if enc == "mpeg4":
        return ["-c:v", "mpeg4", "-q:v", "3"]
    return ["-c:v", enc, "-b:v", "9M"]


@lru_cache(maxsize=None)
def has_filter(name: str) -> bool:
    try:
        rc, out, err = capture(["ffmpeg", "-hide_banner", "-filters"])
    except Exception:
        return False
    return any(line.split()[1:2] == [name] for line in (out + err).splitlines() if len(line.split()) > 1)


# ── probing ─────────────────────────────────────────────────────────────────

@dataclass
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    transfer: str = ""          # colour transfer, e.g. arib-std-b67 (HLG, iPhone HDR) or smpte2084 (PQ)
    screen: object = None       # screen.Track when the clip was filmed off a TV / monitor

    @property
    def hdr(self) -> bool:
        return self.transfer in ("arib-std-b67", "smpte2084")

    @property
    def view_w(self) -> int:
        """Width of the picture the reel is cut from (the straightened screen, if filmed off one)."""
        return self.screen.out_w if self.screen is not None else self.width

    @property
    def view_h(self) -> int:
        return self.screen.out_h if self.screen is not None else self.height

    def pre(self, t0: float | None = None, t1: float | None = None) -> str:
        """Filters for analysis passes: HDR fixed, and only the screen when filmed off one."""
        from . import screen as _screen
        parts = [tonemap_chain(self)]
        if self.screen is not None:
            parts.append(_screen.static_filter(self.screen, t0, t1))
            parts.append(f"scale={self.view_w}:{self.view_h}")
        return ",".join(p for p in parts if p)


def _probe_ffprobe(path: Path) -> VideoInfo:
    rc, out, err = capture(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                            "-show_streams", str(path)])
    if rc != 0:
        raise FFmpegError(err[-1500:] or f"ffprobe failed on {path}")
    data = json.loads(out)
    v = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        raise FFmpegError(f"No video stream in {path}")
    has_audio = any(s.get("codec_type") == "audio" for s in data["streams"])
    dur = float(data["format"].get("duration") or v.get("duration") or 0)
    num, den = (v.get("avg_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = abs(int(sd["rotation"]))
    if rot in (90, 270):
        w, h = h, w
    return VideoInfo(path, dur, w, h, fps, has_audio, str(v.get("color_transfer") or ""))


def _probe_ffmpeg(path: Path) -> VideoInfo:
    """No ffprobe (some iPhone builds): read `ffmpeg -i` output instead."""
    rc, err = _exec(["ffmpeg", "-hide_banner", "-i", str(path)])
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    if not m:
        raise FFmpegError(f"Can't read {path.name}:\n{err[-800:]}")
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    vline = next((l for l in err.splitlines() if "Video:" in l), "")
    sz = re.search(r"\b(\d{2,5})x(\d{2,5})\b", vline)
    if not sz:
        raise FFmpegError(f"No video stream in {path}")
    w, h = int(sz.group(1)), int(sz.group(2))
    fr = re.search(r"([\d.]+)\s*fps", vline)
    fps = float(fr.group(1)) if fr else 30.0
    rot = re.search(r"rotation of\s*(-?[\d.]+)|rotate\s*:\s*(-?\d+)", err)
    if rot and abs(int(float(rot.group(1) or rot.group(2)))) in (90, 270):
        w, h = h, w
    tr = "arib-std-b67" if "arib-std-b67" in vline else "smpte2084" if "smpte2084" in vline else ""
    return VideoInfo(path, dur, w, h, fps, "Audio:" in err, tr)


def probe(path: Path) -> VideoInfo:
    path = Path(path)
    if tool_ok("ffprobe"):
        try:
            return _probe_ffprobe(path)
        except (FFmpegError, ValueError, KeyError):
            pass
    return _probe_ffmpeg(path)


# ── analysis (file-based, no pipes) ─────────────────────────────────────────

def audio_rms(path: Path, window: float = 0.5, sr: int = 8000):
    """RMS loudness (dB) per `window` seconds. Empty array if no audio."""
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "a.raw"
        rc, _ = _exec(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
                       "-f", "s16le", str(raw)])
        data = raw.read_bytes() if raw.exists() else b""
    pcm = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
    hop = int(sr * window)
    n = len(pcm) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = pcm[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-10)
    return (20 * np.log10(rms)).astype(np.float32)


def tonemap_chain(info: "VideoInfo | None") -> str:
    """HDR phone video (iPhone records HLG by default) → normal video. Without this it looks grey and flat."""
    if not info or not info.hdr:
        return ""
    if has_filter("zscale") and has_filter("tonemap"):
        return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    return "eq=contrast=1.12:saturation=1.25:gamma=0.92"     # rough fallback when zscale isn't built in


def rgb_frames(path: Path, fps: float, w: int, h: int, start: float | None = None, dur: float | None = None,
               pre: str = ""):
    """Small colour frames as an (n, h, w, 3) uint8 array."""
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "v.raw"
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if start is not None:
            cmd += ["-ss", f"{max(0.0, start):.3f}"]
        if dur is not None:
            cmd += ["-t", f"{dur:.3f}"]
        vf = f"fps={fps}," + (pre + "," if pre else "") + f"scale={w}:{h},format=rgb24"
        cmd += ["-i", str(path), "-an", "-vf", vf, "-f", "rawvideo", str(raw)]
        _exec(cmd)
        buf = raw.read_bytes() if raw.exists() else b""
    arr = np.frombuffer(buf, dtype=np.uint8)
    n = len(arr) // (w * h * 3)
    return arr[: n * w * h * 3].reshape(n, h, w, 3)


def gray_frames(path: Path, fps: float, w: int, h: int, start: float | None = None, dur: float | None = None,
                pre: str = ""):
    """Small grey frames as an (n, h, w) uint8 array. `pre` = filters to run first (tonemap, perspective)."""
    return _gray_frames(path, fps, w, h, start, dur, pre)


def _gray_frames(path: Path, fps: float, w: int, h: int, start: float | None = None, dur: float | None = None,
                 pre: str = ""):
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "v.raw"
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if start is not None:
            cmd += ["-ss", f"{max(0.0, start):.3f}"]
        if dur is not None:
            cmd += ["-t", f"{dur:.3f}"]
        vf = f"fps={fps}," + (pre + "," if pre else "") + f"scale={w}:{h},format=gray"
        cmd += ["-i", str(path), "-an", "-vf", vf, "-f", "rawvideo", str(raw)]
        _exec(cmd)
        buf = raw.read_bytes() if raw.exists() else b""
    arr = np.frombuffer(buf, dtype=np.uint8)
    n = len(arr) // (w * h)
    return arr[: n * w * h].reshape(n, h, w) if n else arr[:0].reshape(0, h, w)


def motion_profile(path: Path, fps: float = 2.0, pre: str = ""):
    """Per-sample (1/fps s) motion energy and frame detail (std) from tiny grayscale frames."""
    frames = _gray_frames(path, fps, 64, 36, pre=pre).astype(np.float32)
    n = len(frames)
    if n < 2:
        return np.zeros(max(n, 0)), np.zeros(max(n, 0))
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    motion = np.concatenate([[diff[0]], diff])
    detail = frames.std(axis=(1, 2))
    return motion, detail


def tone_stats(path: Path, start: float, dur: float, pre: str = "") -> dict:
    """Brightness of a moment (like Photoshop's histogram): mean, shadows (p2) and highlights (p98), 0-255.
    `pre`: filters to run first (tonemap, the straightened screen) so only the game picture is measured."""
    frames = _gray_frames(path, max(1.0, 8 / max(dur, 1.0)), 96, 54, start, dur, pre)
    if not len(frames):
        return {"mean": 128.0, "p2": 0.0, "p98": 255.0}
    flat = frames.reshape(-1).astype(np.float32)
    return {"mean": float(flat.mean()), "p2": float(np.percentile(flat, 2)), "p98": float(np.percentile(flat, 98))}


def color_stats(path: Path, start: float, dur: float, pre: str = "") -> dict:
    """Per-channel black and white points (p1 / p99, 0-255) and means of a moment, for Auto Color."""
    w, h = 96, 54
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "c.raw"
        vf = f"fps={max(1.0, 8 / max(dur, 1.0))}," + (pre + "," if pre else "") + f"scale={w}:{h},format=rgb24"
        _exec(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}", "-i", str(path),
               "-an", "-vf", vf, "-f", "rawvideo", str(raw)])
        buf = raw.read_bytes() if raw.exists() else b""
    px = np.frombuffer(buf[: len(buf) // 3 * 3], dtype=np.uint8).reshape(-1, 3).astype(np.float32)
    if len(px) < 100:
        return {}
    out = {}
    for i, c in enumerate("rgb"):
        out[c] = {"p1": float(np.percentile(px[:, i], 1)), "p99": float(np.percentile(px[:, i], 99)),
                  "mean": float(px[:, i].mean())}
    return out


def grab_frame(path: Path, t: float, out: Path, width: int = 640, vf_extra: str = "", pre: str = "") -> Path:
    vf = (pre + "," if pre else "") + f"scale={width}:-2" + (f",{vf_extra}" if vf_extra else "")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", vf, "-q:v", "3", str(out)])
    return out


def extract_wav(path: Path, start: float, dur: float, out: Path) -> Path:
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "16000", str(out)])
    return out


# ── Instagram's Reels spec ──────────────────────────────────────────────────
IG_AUDIO_K = 128            # Meta's spec: AAC, 128 kbps, 48 kHz at most
IG_VIDEO_MAX_K = 24000      # 25 Mbps at most; stay a little under


def _streams(path: Path) -> dict:
    rc, so, _ = capture(["ffprobe", "-v", "error", "-show_entries",
                         "stream=codec_type,codec_name,profile,pix_fmt,bit_rate,sample_rate,channels,avg_frame_rate:"
                         "format=duration,bit_rate,size", "-of", "json", str(path)])
    import json as _json
    d = _json.loads(so or "{}") if rc == 0 else {}
    out = {"format": d.get("format") or {}}
    for st in d.get("streams") or []:
        out.setdefault(st.get("codec_type"), st)
    return out


def instagram_ready(src: Path, folder: Path, safe: bool = False) -> tuple[Path, str]:
    """A copy of the reel that matches Instagram's Reels spec, and what was changed ("" = nothing).
    Light: only the audio is re-encoded when it's over 128 kbps / 48 kHz or not AAC (seconds, video copied).
    Safe: everything re-encoded the most conservative way (H.264 Main 4.0, 30 fps, steady bitrate, 2 s GOP),
    for when Instagram refused the file anyway."""
    src = Path(src)
    st = _streams(src)
    a, v, f = st.get("audio") or {}, st.get("video") or {}, st["format"]
    dur = float(f.get("duration") or 0) or probe(src).duration
    out = Path(folder) / ("reel_ig_safe.mp4" if safe else "reel_ig.mp4")
    if safe:
        vk = max(2500, min(6000, int(280 * 8 * 1000 / max(dur, 1)) - IG_AUDIO_K))   # under 280 MB
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
             "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
             "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main", "-level", "4.0", "-pix_fmt", "yuv420p",
             "-b:v", f"{vk}k", "-maxrate", f"{int(vk * 1.3)}k", "-bufsize", f"{vk * 2}k",
             "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
             "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
             "-c:a", "aac", "-b:a", f"{IG_AUDIO_K}k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out)])
        return out, f"re-encoded to Instagram's safest format ({vk // 1000} Mbps, H.264 Main)"
    fixes = []
    vbit = int(v.get("bit_rate") or 0) // 1000
    fps = v.get("avg_frame_rate") or "30/1"
    try:
        n, d = fps.split("/")
        fps_v = float(n) / float(d or 1)
    except (ValueError, ZeroDivisionError):
        fps_v = 30.0
    if v.get("codec_name") not in ("h264", "hevc") or v.get("pix_fmt") != "yuv420p" or vbit > IG_VIDEO_MAX_K \
            or not 23 <= fps_v <= 60:
        return instagram_ready(src, folder, safe=True)
    if a and (a.get("codec_name") != "aac" or int(a.get("bit_rate") or 0) // 1000 > IG_AUDIO_K + 4
              or int(a.get("sample_rate") or 48000) > 48000):
        fixes.append(f"audio {int(a.get('bit_rate') or 0) // 1000} kbps → {IG_AUDIO_K} kbps")
    if not fixes:
        return src, ""
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", f"{IG_AUDIO_K}k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out)])
    return out, ", ".join(fixes)
