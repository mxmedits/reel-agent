"""Turns a moment of 16:9 gameplay into a finished 9:16 reel.

Edit recipe (all tunable in config.yaml):
  1. optional 1.6 s "teaser": the payoff shown first, then the build-up (retention hook)
  2. vertical layout: blurred background fill, zoomed shooter crop, full crop or letterbox
  3. quick punch-zoom on the payoff
  4. big hook text up top, word-by-word captions from your commentary, small handle watermark
  5. mood-matched music with the drop on the payoff and/or AI voiceover (game ducks under it),
     loudness normalised to -14 LUFS
  6. the "Photoshop" pass (looks.py): auto tone, denoise, colour grade, sharpen, glow, vignette,
     grain, impact shake/flash/RGB split on the payoff and a flash on the teaser cut

Reel types (spec.style), all built from the same timeline of segments:
  classic   payoff-first teaser, punch zoom, big hook
  slowmo    classic with the payoff at half speed
  meme_bar  white caption bar on top with a relatable line
  pov       a "POV: ..." line in a caption box that stays up, no teaser
  replay    the moment at full speed, then an instant replay in slow motion with a REPLAY tag
  freeze    freeze-frame just before the payoff with a label, then it plays out
  top3      three moments from one recording counted down #3, #2, #1
  montage   four to six quick cuts of the best moments
Any type can end on an end card (a question on a dimmed last second) to pull comments.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import looks as lk
from . import media, screen

W, H = 1080, 1920
TEASER_PRE, TEASER_POST = 0.9, 0.7


@dataclass
class RenderSpec:
    source: Path
    info: media.VideoInfo
    start: float
    end: float
    peak: float
    layout: str = "blur"
    hook: str = ""
    handle: str = ""
    words: list = field(default_factory=list)   # [{'w','s','e'}] relative to start
    teaser: bool = True
    punch: bool = True
    fps: int = 30
    crf: int = 20
    preset: str = "veryfast"
    font: str = "Anton"
    music: Path | None = None
    music_volume: float = 0.55
    game_volume: float = 0.6           # game audio level while music plays
    music_seek: float = 0.0            # start this far into the track...
    music_delay: float = 0.0           # ...or wait this long before it starts (drop lands on payoff)
    voice: Path | None = None          # voiceover track aligned to the main part (starts at self.start)
    style: str = "classic"             # one of STYLES below (picked per clip, see formats.py)
    meme_line: str = ""                # the line for the meme_bar caption bar or the POV box
    look: lk.Look | None = None        # colour grade + retouch + impact FX (looks.plan)
    encoder: str = "auto"              # auto = libx264, or the iPhone's hardware encoder
    moments: list = field(default_factory=list)   # [{'start','end','peak'}] best first, for top3 / montage
    freeze_line: str = ""              # label on the freeze frame
    end_card: str = ""                 # question shown on the dimmed last second ("RATE THIS /10")
    cut_seconds: float = 2.0           # montage cut length (learned from top reels)
    text_scale: float = 1.0            # all on-screen text bigger (>1) or smaller (<1)
    game_volume_alone: float = 1.0     # game sound level when there is no music (0 = muted)


STYLES = ("classic", "slowmo", "meme_bar", "pov", "replay", "freeze", "top3", "montage")
COMPILATIONS = {"top3": 3, "montage": 4}       # style -> moments it needs


@dataclass
class Seg:
    """One stretch of the finished reel."""
    start: float                       # where it starts in the recording
    dur: float                         # how much of the recording it uses (or how long a freeze holds)
    kind: str = "main"                 # teaser | main | replay | freeze | rank | cut
    speed: float = 1.0                 # 0.5 = whole segment in slow motion
    freeze: bool = False
    slow: tuple | None = None          # (a, b) inside the segment played at half speed
    punch_at: float | None = None
    impact_at: float | None = None     # in the segment's own output time
    cut_flash: bool = False
    zoom: float = 1.0
    hook_until: float = 0.0            # show the hook from 0 to here (0 = no hook)
    words: list = field(default_factory=list)
    badge: str = ""                    # small tag top-left for the whole segment ("REPLAY")
    rank: str = ""                     # big "#1" style number
    label: str = ""                    # big centred label (freeze frame)
    end_card: str = ""
    end_card_at: float = 0.0

    @property
    def out(self) -> float:
        d = self.dur / self.speed
        return d + ((self.slow[1] - self.slow[0]) if self.slow else 0.0)


# ── ASS subtitle file (hook + captions + watermark + tags) ──────────────────

def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text: str) -> str:
    return str(text).replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def _wrap(text: str, width: int) -> str:
    """Break a line for libass (\\N) so long sentences stay inside the safe zone."""
    words, lines, cur = _clean(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return r"\N".join(lines[:4])


def build_ass(font: str, duration: float, hook: str, hook_until: float, handle: str,
              words: list, words_offset: float = 0.0, style: str = "classic", meme_line: str = "",
              seg: "Seg | None" = None) -> str:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font},172,&H00FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,7,3,8,100,230,320,1
Style: Sub,{font},128,&H00FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,6,2,2,100,230,500,1
Style: Handle,{font},48,&H40FFFFFF,&H000000FF,&H90000000,&H00000000,-1,0,0,0,100,100,3,0,1,2,0,8,100,230,250,1
Style: HookBar,{font},104,&H00141414,&H000000FF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,1,0,0,5,80,80,0,1
Style: HandleBar,{font},40,&H00808080,&H000000FF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,2,0,1,0,0,8,80,80,30,1
Style: Pov,{font},88,&H00FFFFFF,&H000000FF,&H46101010,&H46101010,0,0,0,0,100,100,1,0,3,18,0,8,110,240,330,1
Style: Badge,{font},72,&H00FFFFFF,&H000000FF,&H003C32F0,&H003C32F0,0,0,0,0,100,100,3,0,3,14,0,7,124,230,330,1
Style: Rank,{font},270,&H0000E6FF,&H000000FF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,9,4,7,90,230,300,1
Style: Label,{font},150,&H0000E6FF,&H000000FF,&H00000000,&H78000000,0,0,0,0,100,100,1,0,1,8,3,5,100,230,0,1
Style: EndCard,{font},170,&H00FFFFFF,&H000000FF,&H00000000,&H78000000,0,0,0,0,100,100,1,0,1,8,3,5,100,230,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    pop = r"{\fad(60,220)\fscx70\fscy70\t(0,140,\fscx106\fscy106)\t(140,220,\fscx100\fscy100)}"
    if style == "meme_bar":
        if handle:
            ev.append(f"Dialogue: 0,{_ts(0)},{_ts(duration)},HandleBar,,0,0,0,,{_clean(handle)}")
        line = (meme_line or hook).strip()
        if line:   # meme format: the line stays up the whole time, sentence case, centred in the white bar
            ev.append(f"Dialogue: 2,{_ts(0)},{_ts(duration)},HookBar,,0,0,0,,{{\\an5\\pos(540,262)}}{_wrap(line, 24)}")
        hook_until = 0.0
    elif style == "pov":
        if handle:
            ev.append(f"Dialogue: 0,{_ts(0)},{_ts(duration)},Handle,,0,0,0,,{_clean(handle)}")
        line = (meme_line or hook).strip()
        if line:   # POV format: a caption box that stays up, the way people type them in the app
            ev.append(f"Dialogue: 2,{_ts(0)},{_ts(duration)},Pov,,0,0,0,,{{\\fad(120,0)}}{_wrap(line, 26)}")
        hook_until = 0.0
    else:
        if handle:
            ev.append(f"Dialogue: 0,{_ts(0)},{_ts(duration)},Handle,,0,0,0,,{_clean(handle)}")
        if hook and hook_until > 0:
            ev.append(f"Dialogue: 2,{_ts(0)},{_ts(min(hook_until, duration))},Hook,,0,0,0,,{pop}{_wrap(hook.upper(), 14)}")
    if seg is not None:
        hook_on = hook_until > 0 and style not in ("meme_bar", "pov")
        if seg.rank:   # the countdown number sits under the hook while the hook is up
            y = 640 if hook_on else 300
            ev.append(f"Dialogue: 3,{_ts(0)},{_ts(duration)},Rank,,0,0,0,,{{\\pos(112,{y})}}{pop}{_clean(seg.rank)}")
        if seg.badge:
            ev.append(f"Dialogue: 3,{_ts(0)},{_ts(duration)},Badge,,0,0,0,,{{\\fad(80,0)}}{_clean(seg.badge).upper()}")
        if seg.label:
            ev.append(f"Dialogue: 3,{_ts(0.05)},{_ts(duration)},Label,,0,0,0,,{{\\pos(480,740)}}{pop}"
                      f"{_wrap(seg.label.upper(), 15)}")
        if seg.end_card:
            ev.append(f"Dialogue: 4,{_ts(seg.end_card_at)},{_ts(duration)},EndCard,,0,0,0,,{{\\pos(480,860)}}{pop}"
                      f"{_wrap(seg.end_card.upper(), 14)}")
    for chunk in _chunks(words):
        for i, w in enumerate(chunk):
            s = w["s"] + words_offset
            e = (chunk[i + 1]["s"] if i + 1 < len(chunk) else max(w["e"], w["s"] + 0.25)) + words_offset
            if s >= duration:
                continue
            if s < hook_until:   # don't stack captions on the hook
                continue
            if seg is not None and seg.end_card and s >= seg.end_card_at:
                continue
            parts = []
            for j, x in enumerate(chunk):
                txt = _clean(x["w"]).upper()
                parts.append(r"{\c&H0000E6FF&}" + txt + r"{\c&H00FFFFFF&}" if j == i else txt)
            ev.append(f"Dialogue: 1,{_ts(s)},{_ts(min(e, duration))},Sub,,0,0,0,,{' '.join(parts)}")
    return head + "\n".join(ev) + "\n"


def scale_text(ass: str, scale: float) -> str:
    """Every text style bigger or smaller (the owner's 'bigger text' note)."""
    if abs(scale - 1.0) < 0.01:
        return ass
    return re.sub(r"^(Style: \w+,[^,]+,)(\d+),", lambda m: f"{m.group(1)}{int(round(int(m.group(2)) * scale))},",
                  ass, flags=re.M)


def _chunks(words: list, max_words: int = 3, max_gap: float = 0.6) -> list[list]:
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["s"] - cur[-1]["e"] > max_gap):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


# ── ffmpeg filter graph ─────────────────────────────────────────────────────

def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def _video_graph(info: media.VideoInfo, layout: str, dur: float, punch_at: float | None,
                 fps: int, ass_name: str | None, style: str = "classic", slow: tuple | None = None,
                 look: lk.Look | None = None, impact_at: float | None = None,
                 cut_flash_at: float | None = None, seg: "Seg | None" = None, src_chain: str = "") -> str:
    """Video graph ending in [vout]. With `slow` or a slow segment, it also builds the audio ([aout]).
    `src_chain`: filters applied to the source first (HDR fix, straightening a filmed screen)."""
    sw, sh = info.view_w, info.view_h
    smooth = f"framerate=fps={fps}," if info.fps < 50 and media.has_filter("framerate") else ""
    speed = seg.speed if seg else 1.0
    if slow:   # slow motion on the payoff: video at half speed (60 fps sources stay smooth), audio pitch kept
        a, b = slow
        g = [
            "[0:v]split=3[sa][sb][sc]",
            f"[sa]trim=end={a:.3f},setpts=PTS-STARTPTS,setsar=1[s1]",
            f"[sb]trim=start={a:.3f}:end={b:.3f},setpts=2.0*(PTS-STARTPTS),{smooth}"
            f"crop={_even(sw * 0.86)}:{_even(sh * 0.86)},scale={sw}:{sh},setsar=1[s2]",
            f"[sc]trim=start={b:.3f},setpts=PTS-STARTPTS,setsar=1[s3]",
            "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=3[xa][xb][xc]",
            f"[xa]atrim=end={a:.3f},asetpts=PTS-STARTPTS[x1]",
            f"[xb]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,atempo=0.5[x2]",
            f"[xc]atrim=start={b:.3f},asetpts=PTS-STARTPTS[x3]",
            "[s1][x1][s2][x2][s3][x3]concat=n=3:v=1:a=1[cv][aout]",
            f"[cv]fps={fps},setsar=1[src]",
        ]
        punch_at = None
    elif speed != 1.0:   # the whole segment slowed down (instant replay)
        g = [f"[0:v]setpts={1 / speed:.3f}*PTS,{smooth}fps={fps},setsar=1[src]"]
    else:
        g = [f"[0:v]fps={fps},setsar=1[src]"]
    cur = "src"
    pre = list(look.source_chain) if look and look.source_chain else []   # grade the source so the blurred fill matches
    if seg and seg.zoom != 1.0:
        pre.append(f"crop={_even(sw * seg.zoom)}:{_even(sh * seg.zoom)},scale={sw}:{sh}")
    if seg and seg.freeze:                                # a paused frame: drained of colour, slightly dark
        pre.append("eq=saturation=0.3:brightness=-0.05:contrast=1.08")
    if pre:
        g.append(f"[src]{','.join(pre)}[srcl]")
        cur = "srcl"

    if punch_at is not None:
        a, b = punch_at - 0.08, punch_at + 0.55
        if a > 0.15 and b < dur - 0.15:
            g += [
                f"[{cur}]split=3[pa][pb][pc]",
                f"[pa]trim=end={a:.3f},setpts=PTS-STARTPTS[pa1]",
                f"[pb]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,"
                f"crop={_even(sw * 0.82)}:{_even(sh * 0.82)},scale={sw}:{sh},setsar=1[pb1]",
                f"[pc]trim=start={b:.3f},setpts=PTS-STARTPTS[pc1]",
                "[pa1][pb1][pc1]concat=n=3:v=1:a=0[pz]",
            ]
            cur = "pz"

    portrait = sw / sh < 0.8
    if layout == "zoom" and not portrait:
        cw, ch = min(sw, _even(sh * 4 / 5)), sh       # shooter crop: centre 4:5 keeps crosshair + action
        fg_crop = f"crop={cw}:{ch},"
    else:
        cw, ch, fg_crop = sw, sh, ""

    if style == "meme_bar":                           # white caption bar on top, video under it
        bar = 520
        avail = H - bar
        fw, fh = W, _even(W * ch / cw)
        if fh > avail:
            fh, fw = avail, _even(avail * cw / ch)
        g.append(f"[{cur}]{fg_crop}scale={fw}:{fh}:flags=lanczos,pad={W}:{H}:(ow-iw)/2:{bar}:color=black,"
                 f"drawbox=x=0:y=0:w={W}:h={bar}:color=white:t=fill,setsar=1[vl]")
    elif layout == "crop" and not portrait:
        cw, ch = min(sw, _even(sh * 9 / 16)), min(sh, _even(sw * 16 / 9))
        g.append(f"[{cur}]crop={cw}:{ch},scale={W}:{H}:flags=lanczos,setsar=1[vl]")
    else:
        fw, fh = W, _even(W * ch / cw)
        if fh > H:
            fh, fw = H, _even(H * cw / ch)
        y = f"(H-h)/2{'-60' if layout == 'blur' and fh < H * 0.5 else ''}"
        if layout == "fit":
            g.append(f"[{cur}]{fg_crop}scale={fw}:{fh}:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,"
                     "setsar=1[vl]")
        else:
            g += [
                f"[{cur}]split=2[bgs][fgs]",
                f"[bgs]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,"
                f"scale={W}:{H},eq=brightness=-0.16:saturation=0.95[bg]",
                f"[fgs]{fg_crop}scale={fw}:{fh}:flags=lanczos[fg]",
                f"[bg][fg]overlay=(W-w)/2:{y},setsar=1[vl]",
            ]
    if src_chain:                                     # everything above reads the fixed-up source
        g = [line.replace("[0:v]", "[v0]") for line in g]
        g.insert(0, f"[0:v]{src_chain}[v0]")
    last = "vl"
    if look and (look.name != "none" or look.flash or look.shake or look.cut_flash):
        g += lk.canvas_chain(look, "vl", "vfx", W, H, impact_at, cut_flash_at)
        last = "vfx"
    extra = []
    if seg and seg.freeze:                               # pause bars, centred in the picture
        extra += [f"drawbox=x={W // 2 - 62}:y=860:w=44:h=150:color=white@0.9:t=fill",
                  f"drawbox=x={W // 2 + 18}:y=860:w=44:h=150:color=white@0.9:t=fill"]
    if seg and seg.end_card:                             # dim the last second so the question reads
        extra.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.5:t=fill:enable='gte(t,{seg.end_card_at:.3f})'")
    if extra:
        g.append(f"[{last}]{','.join(extra)}[vx]")
        last = "vx"
    if ass_name:   # text goes on last so it never shakes, blurs or gets graded
        g.append(f"[{last}]ass={ass_name}:fontsdir=fonts,format=yuv420p[vout]")
    else:
        g.append(f"[{last}]format=yuv420p[vout]")
    return ";".join(g)


def _render_seg(spec: RenderSpec, seg: Seg, i: int, workdir: Path) -> Path:
    out = workdir / f"p{i}_{seg.kind}.mp4"
    out_dur = seg.out
    ass_name = out.stem + ".ass"
    (workdir / ass_name).write_text(
        scale_text(build_ass(spec.font, out_dur, spec.hook, seg.hook_until, spec.handle, seg.words,
                             style=spec.style, meme_line=spec.meme_line, seg=seg), spec.text_scale), encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-v", "error"]
    audio_in_graph = bool(seg.slow)
    info = spec.info
    tonemap = media.tonemap_chain(info)
    if seg.freeze:
        still = workdir / f"p{i}_still.png"
        media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(seg.start, 0):.3f}", "-i", str(spec.source),
                   *(["-vf", tonemap] if tonemap else []), "-frames:v", "1", still.name], cwd=workdir)
        cmd += ["-loop", "1", "-framerate", str(spec.fps), "-t", f"{seg.dur:.3f}", "-i", still.name]
        src_chain = screen.segment_filter(spec.source, info, info.screen, seg.start, 0.0, spec.fps, still=True) \
            if info.screen is not None else ""
    else:
        cmd += ["-ss", f"{seg.start:.3f}", "-t", f"{seg.dur:.3f}", "-i", str(spec.source)]
        parts = [tonemap]
        if info.screen is not None:
            parts.append(screen.segment_filter(spec.source, info, info.screen, seg.start, seg.dur, spec.fps))
        src_chain = ",".join(x for x in parts if x)
    graph = _video_graph(info, spec.layout, seg.dur, seg.punch_at, spec.fps, ass_name, spec.style, seg.slow,
                         spec.look, seg.impact_at, 0.0 if seg.cut_flash else None, seg=seg, src_chain=src_chain)
    if not audio_in_graph:
        fmt = "aresample=48000,aformat=channel_layouts=stereo"
        if spec.info.has_audio and not seg.freeze:
            tempo = f",atempo={seg.speed:.3f}" if seg.speed != 1.0 else ""
            graph += f";[0:a]{fmt}{tempo}[aout]"
        else:
            cmd += ["-f", "lavfi", "-t", f"{out_dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            graph += ";[1:a]anull[aout]"
    cmd += media.graph_args(graph, workdir, out.stem + "_graph.txt")
    cmd += ["-map", "[vout]", "-map", "[aout]", *media.encode_args(spec.crf, spec.preset, spec.encoder),
            "-r", str(spec.fps), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-t", f"{out_dur:.3f}", out.name]
    media.run(cmd, cwd=workdir)   # run inside workdir so the .ass / font paths need no escaping
    return out


def _stretch(t: float, slow: tuple | None) -> float:
    """Where a moment of the original main part lands after slow motion."""
    if not slow:
        return t
    a, b = slow
    if t <= a:
        return t
    if t <= b:
        return a + 2 * (t - a)
    return t + (b - a)


# ── the timeline for each reel type ─────────────────────────────────────────

def _words_for(spec: RenderSpec, src_from: float, src_to: float, slow: tuple | None = None) -> list:
    """Caption words (timed from spec.start) that fall inside one segment, re-timed to that segment."""
    lo, hi = src_from - spec.start, src_to - spec.start
    out = []
    for w in spec.words:
        if lo <= w["s"] < hi:
            out.append({**w, "s": _stretch(w["s"] - lo, slow), "e": _stretch(w["e"] - lo, slow)})
    return out


def timeline(spec: RenderSpec) -> dict:
    """Plan the reel: segments, where the payoff lands, the cover frame. No rendering.
    Falls back to `classic` whenever a type's needs aren't met, and says so in the result."""
    style = spec.style if spec.style in STYLES else "classic"
    dur_src = spec.info.duration
    main_dur = spec.end - spec.start
    p = spec.peak - spec.start
    fx = 0.25 if spec.look and (spec.look.flash or spec.look.rgb_split) else 0.0
    why = ""

    moments = [m for m in spec.moments if m.get("peak") is not None]
    if style in COMPILATIONS and len(moments) < COMPILATIONS[style]:
        style, why = "classic", "not enough strong moments for a compilation"
    if style == "pov" and not (spec.meme_line or "").strip():
        style, why = "classic", "no POV line"
    if style == "replay" and not (p >= 2.0 and spec.peak + 0.9 <= dur_src):
        style, why = "classic", "payoff too close to an edge for a replay"
    if style == "freeze" and (spec.voice or not (p > 2.5 and main_dur - p > 1.0)):
        style, why = "classic", "freeze doesn't fit this moment"
    slow = None
    if style == "slowmo":
        a, b = p - 0.5, p + 0.7
        if spec.info.has_audio and not spec.voice and a > 0.3 and b < main_dur - 0.3:
            slow = (round(a, 3), round(b, 3))
        else:
            style, why = "classic", "payoff too close to an edge for slow motion"

    segs: list[Seg] = []
    voice_at = 0.0
    if style == "top3":
        picks = sorted(moments[:3], key=lambda m: m.get("score", 0))            # weakest first: #3, #2, #1
        for n, m in enumerate(picks):
            s = max(0.0, m["peak"] - 3.4)
            e = min(dur_src, m["peak"] + 1.8)
            segs.append(Seg(start=s, dur=e - s, kind="rank", rank=f"#{3 - n}", impact_at=m["peak"] - s,
                            punch_at=(m["peak"] - s) if (n == 2 and spec.punch) else None,
                            cut_flash=n > 0, hook_until=2.2 if n == 0 else 0.0))
        payoff = sum(x.out for x in segs[:2]) + segs[2].impact_at
        cover = payoff + fx
    elif style == "montage":
        cut = min(3.0, max(1.2, float(spec.cut_seconds or 2.0)))
        picks = sorted(moments[:6], key=lambda m: m["peak"])                    # in the order they happened
        best = max(picks, key=lambda m: m.get("score", 0))
        picks = [m for m in picks if m is not best] + [best]                    # the best one lands last
        for n, m in enumerate(picks):
            s = max(0.0, m["peak"] - cut * 0.62)
            e = min(dur_src, s + cut + (0.9 if n == len(picks) - 1 else 0.0))
            segs.append(Seg(start=s, dur=e - s, kind="cut", cut_flash=n > 0,
                            impact_at=(m["peak"] - s) if n == len(picks) - 1 else None,
                            hook_until=min(1.9, e - s) if n == 0 else 0.0))
        payoff = sum(x.out for x in segs[:-1]) + (segs[-1].impact_at or 0)
        cover = min(0.9, segs[0].out - 0.1)                                     # first cut, hook on screen
    elif style == "replay":
        end = min(spec.end, spec.peak + 1.4)
        segs.append(Seg(start=spec.start, dur=end - spec.start, kind="main", hook_until=2.6,
                        punch_at=p if spec.punch else None, impact_at=p,
                        words=_words_for(spec, spec.start, end)))
        r0 = spec.peak - 1.1
        segs.append(Seg(start=r0, dur=2.0, kind="replay", speed=0.5, zoom=0.86, badge="Replay", cut_flash=True,
                        impact_at=1.1 / 0.5))
        payoff = p
        cover = p + fx
    elif style == "freeze":
        f = p - 0.45                                                             # just before it happens
        hold = 1.3
        segs.append(Seg(start=spec.start, dur=f, kind="main", hook_until=min(2.6, f),
                        words=_words_for(spec, spec.start, spec.start + f)))
        segs.append(Seg(start=spec.start + f, dur=hold, kind="freeze", freeze=True,
                        label=spec.freeze_line or "Watch this"))
        rest = main_dur - f
        segs.append(Seg(start=spec.start + f, dur=rest, kind="main", punch_at=(p - f) if spec.punch else None,
                        impact_at=p - f, cut_flash=True, words=_words_for(spec, spec.start + f, spec.end)))
        payoff = f + hold + (p - f)
        cover = f + 0.7                                                          # the labelled freeze frame
    else:   # classic, slowmo, meme_bar, pov
        teaser_len = 0.0
        if spec.teaser and p > 4 and style != "pov":
            t0 = max(0.0, spec.peak - TEASER_PRE)
            t1 = min(dur_src, spec.peak + TEASER_POST)
            teaser_len = t1 - t0
            segs.append(Seg(start=t0, dur=teaser_len, kind="teaser", hook_until=teaser_len + 1,
                            impact_at=spec.peak - t0))
        segs.append(Seg(start=spec.start, dur=main_dur, kind="main", slow=slow,
                        hook_until=1.8 if teaser_len else 2.6,
                        punch_at=p if spec.punch else None, impact_at=_stretch(p, slow),
                        cut_flash=bool(teaser_len), words=_words_for(spec, spec.start, spec.end, slow)))
        voice_at = teaser_len
        payoff = teaser_len + _stretch(p, slow)
        # cover = the payoff with the hook title on it (inside the teaser when there is one), a beat after
        # the impact flash so Instagram's thumbnail is never a white frame
        cover = min(spec.peak - max(0.0, spec.peak - TEASER_PRE) + fx, teaser_len - 0.1) if teaser_len \
            else _stretch(p, slow) + fx

    if (spec.end_card or "").strip() and segs:
        lastseg = segs[-1]
        room = lastseg.out * 0.45 if lastseg.impact_at is None else lastseg.out - (lastseg.impact_at + 0.6)
        card = min(1.5, room)                                                # never cover the payoff itself
        if card >= 0.9:
            lastseg.end_card, lastseg.end_card_at = spec.end_card.strip(), round(lastseg.out - card, 3)
    total = sum(x.out for x in segs)
    return {"style": style, "segs": segs, "payoff": round(payoff, 3), "cover": round(max(0.0, min(cover, total - 0.1)), 3),
            "total": round(total, 3), "voice_at": voice_at, "fallback": why}


def payoff_time(spec: RenderSpec) -> float:
    """Seconds into the finished reel where the big moment lands (music drops are lined up on this)."""
    return timeline(spec)["payoff"]


def render(spec: RenderSpec, out_path: Path) -> dict:
    """Render the reel to out_path. Returns {'duration', 'cover_ms', 'style', ...}."""
    spec.source = Path(spec.source).resolve()
    out_path = Path(out_path).resolve()
    if spec.music:
        spec.music = Path(spec.music).resolve()
    if spec.voice:
        spec.voice = Path(spec.voice).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plan = timeline(spec)
    asked, spec.style = spec.style, plan["style"]
    segs: list[Seg] = plan["segs"]

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        (wd / "fonts").mkdir()
        for f in media.FONTS.glob("*.[ot]tf"):          # bundled caption font (Anton), same on every device
            shutil.copy(f, wd / "fonts" / f.name)
        parts = [_render_seg(spec, seg, i, wd) for i, seg in enumerate(segs)]

        joined = wd / "joined.mp4"
        if len(parts) == 1:
            joined = parts[0]
        else:
            (wd / "list.txt").write_text("".join(f"file '{x.name}'\n" for x in parts), encoding="utf-8")
            media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
                       "-c", "copy", joined.name], cwd=wd)

        # final pass: game + music bed, voiceover on top (bed ducks under it), loudness. Video copied untouched.
        total = plan["total"]
        fmt = "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(joined)]
        chains = [f"[0:a]{fmt},volume={spec.game_volume if spec.music else spec.game_volume_alone}[g0]"]
        bed, idx = "[g0]", 1
        if spec.music:
            cmd += ["-stream_loop", "-1", "-ss", f"{spec.music_seek:.2f}", "-i", str(spec.music)]
            delay = int(spec.music_delay * 1000)
            chains.append(f"[{idx}:a]{fmt},volume={spec.music_volume}"
                          + (f",adelay={delay}|{delay}" if delay else "") + ",afade=t=in:d=0.4[m]")
            chains.append("[g0][m]amix=inputs=2:duration=first:dropout_transition=0,volume=2[bed]")
            bed, idx = "[bed]", idx + 1
        if spec.voice:
            cmd += ["-i", str(spec.voice)]
            ms = int(plan["voice_at"] * 1000)
            chains.append(f"[{idx}:a]{fmt},adelay={ms}|{ms},volume=1.7,apad,asplit=2[vsc][vmx]")
            chains.append(f"{bed}[vsc]sidechaincompress=threshold=0.015:ratio=12:attack=15:release=400[bd]")
            chains.append("[bd][vmx]amix=inputs=2:duration=first:dropout_transition=0,volume=2[mx]")
            bed, idx = "[mx]", idx + 1
        chains.append(f"{bed}afade=t=out:st={max(0.0, total - 0.6):.2f}:d=0.6,"
                      "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")
        cmd += media.graph_args(";".join(chains), wd, "mix_graph.txt")
        cmd += ["-map", "0:v", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", "-t", f"{total:.3f}", str(out_path)]
        media.run(cmd, cwd=wd)

    final = media.probe(out_path)
    return {"duration": round(final.duration, 2), "cover_ms": int(plan["cover"] * 1000), "style": spec.style,
            "asked_style": asked, "fallback": plan["fallback"], "payoff": plan["payoff"],
            "cuts": len(segs), "end_card": bool(segs and segs[-1].end_card),
            "fx": spec.look.to_dict() if spec.look else None, "encoder": media.video_encoder(spec.encoder)}
