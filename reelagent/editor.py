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
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import looks as lk
from . import media

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
    style: str = "classic"             # classic | slowmo | meme_bar  (picked per clip from the trend brief)
    meme_line: str = ""                # text for the meme_bar caption bar
    look: lk.Look | None = None        # colour grade + retouch + impact FX (looks.plan)
    encoder: str = "auto"              # auto = libx264, or the iPhone's hardware encoder


# ── ASS subtitle file (hook + captions + watermark) ─────────────────────────

def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def build_ass(font: str, duration: float, hook: str, hook_until: float, handle: str,
              words: list, words_offset: float = 0.0, style: str = "classic", meme_line: str = "") -> str:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font},112,&H00FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,7,3,8,100,230,320,1
Style: Sub,{font},96,&H00FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,6,2,2,100,230,500,1
Style: Handle,{font},40,&H40FFFFFF,&H000000FF,&H90000000,&H00000000,-1,0,0,0,100,100,3,0,1,2,0,8,100,230,250,1
Style: HookBar,{font},80,&H00141414,&H000000FF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,1,0,0,5,80,80,0,1
Style: HandleBar,{font},32,&H00808080,&H000000FF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,2,0,1,0,0,8,80,80,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if style == "meme_bar":
        if handle:
            ev.append(f"Dialogue: 0,{_ts(0)},{_ts(duration)},HandleBar,,0,0,0,,{_clean(handle)}")
        line = (meme_line or hook).strip()
        if line:   # meme format: the line stays up the whole time, sentence case, centred in the white bar
            ev.append(f"Dialogue: 2,{_ts(0)},{_ts(duration)},HookBar,,0,0,0,,{{\\an5\\pos(540,262)}}{_clean(line)}")
        hook_until = 0.0
    else:
        if handle:
            ev.append(f"Dialogue: 0,{_ts(0)},{_ts(duration)},Handle,,0,0,0,,{_clean(handle)}")
        if hook:
            pop = r"{\fad(60,220)\fscx70\fscy70\t(0,140,\fscx106\fscy106)\t(140,220,\fscx100\fscy100)}"
            ev.append(f"Dialogue: 2,{_ts(0)},{_ts(min(hook_until, duration))},Hook,,0,0,0,,{pop}{_clean(hook).upper()}")
    for chunk in _chunks(words):
        for i, w in enumerate(chunk):
            s = w["s"] + words_offset
            e = (chunk[i + 1]["s"] if i + 1 < len(chunk) else max(w["e"], w["s"] + 0.25)) + words_offset
            if s >= duration:
                continue
            if s < hook_until:   # don't stack captions on the hook
                continue
            parts = []
            for j, x in enumerate(chunk):
                txt = _clean(x["w"]).upper()
                parts.append(r"{\c&H0000E6FF&}" + txt + r"{\c&H00FFFFFF&}" if j == i else txt)
            ev.append(f"Dialogue: 1,{_ts(s)},{_ts(min(e, duration))},Sub,,0,0,0,,{' '.join(parts)}")
    return head + "\n".join(ev) + "\n"


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
                 cut_flash_at: float | None = None) -> str:
    """Video graph ending in [vout]. With `slow`, it also builds the audio and ends in [aout]."""
    sw, sh = info.width, info.height
    if slow:   # slow motion on the payoff: video at half speed (60 fps sources stay smooth), audio pitch kept
        a, b = slow
        g = [
            "[0:v]split=3[sa][sb][sc]",
            f"[sa]trim=end={a:.3f},setpts=PTS-STARTPTS,setsar=1[s1]",
            f"[sb]trim=start={a:.3f}:end={b:.3f},setpts=2.0*(PTS-STARTPTS),"
            + (f"framerate=fps={fps}," if info.fps < 50 and media.has_filter("framerate") else "")
            + f"crop={_even(sw * 0.86)}:{_even(sh * 0.86)},scale={sw}:{sh},setsar=1[s2]",
            f"[sc]trim=start={b:.3f},setpts=PTS-STARTPTS,setsar=1[s3]",
            "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=3[xa][xb][xc]",
            f"[xa]atrim=end={a:.3f},asetpts=PTS-STARTPTS[x1]",
            f"[xb]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,atempo=0.5[x2]",
            f"[xc]atrim=start={b:.3f},asetpts=PTS-STARTPTS[x3]",
            "[s1][x1][s2][x2][s3][x3]concat=n=3:v=1:a=1[cv][aout]",
            f"[cv]fps={fps},setsar=1[src]",
        ]
        punch_at = None
    else:
        g = [f"[0:v]fps={fps},setsar=1[src]"]
    cur = "src"
    if look and look.source_chain:                    # grade the source so the blurred fill matches
        g.append(f"[src]{','.join(look.source_chain)}[srcl]")
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
                f"scale={W}:{H},eq=brightness=-0.07:saturation=1.2[bg]",
                f"[fgs]{fg_crop}scale={fw}:{fh}:flags=lanczos[fg]",
                f"[bg][fg]overlay=(W-w)/2:{y},setsar=1[vl]",
            ]
    last = "vl"
    if look and (look.name != "none" or look.flash or look.shake or look.cut_flash):
        g += lk.canvas_chain(look, "vl", "vfx", W, H, impact_at, cut_flash_at)
        last = "vfx"
    if ass_name:   # text goes on last so it never shakes, blurs or gets graded
        g.append(f"[{last}]ass={ass_name}:fontsdir=fonts,format=yuv420p[vout]")
    else:
        g.append(f"[{last}]format=yuv420p[vout]")
    return ";".join(g)


def _render_part(spec: RenderSpec, start: float, dur: float, punch_at: float | None,
                 ass_text: str | None, out: Path, workdir: Path, slow: tuple | None = None,
                 impact_at: float | None = None, cut_flash_at: float | None = None) -> Path:
    ass_name = None
    if ass_text:
        ass_name = out.stem + ".ass"
        (workdir / ass_name).write_text(ass_text, encoding="utf-8")
    out_dur = dur + ((slow[1] - slow[0]) if slow else 0.0)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(spec.source)]
    graph = _video_graph(spec.info, spec.layout, dur, punch_at, spec.fps, ass_name, spec.style, slow,
                         spec.look, impact_at, cut_flash_at)
    if not slow:
        if spec.info.has_audio:
            graph += ";[0:a]aresample=48000,aformat=channel_layouts=stereo[aout]"
        else:
            cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
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


def render(spec: RenderSpec, out_path: Path) -> dict:
    """Render the reel to out_path. Returns {'duration', 'cover_ms'}."""
    spec.source = Path(spec.source).resolve()
    out_path = Path(out_path).resolve()
    if spec.music:
        spec.music = Path(spec.music).resolve()
    if spec.voice:
        spec.voice = Path(spec.voice).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if spec.style not in ("classic", "slowmo", "meme_bar"):
        spec.style = "classic"
    main_dur = spec.end - spec.start
    p = spec.peak - spec.start                       # payoff, relative to main part
    hook_until = 2.6
    slow = None
    if spec.style == "slowmo" and spec.info.has_audio and not spec.voice:
        a, b = p - 0.5, p + 0.7
        if a > 0.3 and b < main_dur - 0.3:
            slow = (round(a, 3), round(b, 3))
    if spec.style == "slowmo" and not slow:
        spec.style = "classic"                       # payoff too close to an edge (or voiceover): stay classic
    main_eff = main_dur + ((slow[1] - slow[0]) if slow else 0.0)
    words = [{**w, "s": _stretch(w["s"], slow), "e": _stretch(w["e"], slow)} for w in spec.words]

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        (wd / "fonts").mkdir()
        for f in media.FONTS.glob("*.[ot]tf"):          # bundled caption font (Anton), same on every device
            shutil.copy(f, wd / "fonts" / f.name)
        parts = []
        teaser_len = 0.0
        if spec.teaser and p > 4:
            t0 = max(0.0, spec.peak - TEASER_PRE)
            t1 = min(spec.info.duration, spec.peak + TEASER_POST)
            teaser_len = t1 - t0
            ass = build_ass(spec.font, teaser_len, spec.hook, teaser_len + 1, spec.handle, [],
                            style=spec.style, meme_line=spec.meme_line)
            parts.append(_render_part(spec, t0, teaser_len, None, ass, wd / "p0_teaser.mp4", wd,
                                      impact_at=spec.peak - t0))
        ass = build_ass(spec.font, main_eff, spec.hook, hook_until if not teaser_len else 1.8,
                        spec.handle, words, style=spec.style, meme_line=spec.meme_line)
        parts.append(_render_part(spec, spec.start, main_dur, p if spec.punch else None, ass,
                                  wd / "p1_main.mp4", wd, slow=slow, impact_at=_stretch(p, slow),
                                  cut_flash_at=0.0 if teaser_len else None))

        joined = wd / "joined.mp4"
        if len(parts) == 1:
            joined = parts[0]
        else:
            (wd / "list.txt").write_text("".join(f"file '{x.name}'\n" for x in parts), encoding="utf-8")
            media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
                       "-c", "copy", joined.name], cwd=wd)

        # final pass: game + music bed, voiceover on top (bed ducks under it), loudness. Video copied untouched.
        total = teaser_len + main_eff
        fmt = "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(joined)]
        chains = [f"[0:a]{fmt},volume={spec.game_volume if spec.music else 1.0}[g0]"]
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
            ms = int(teaser_len * 1000)
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
    # cover = the payoff with the hook title on it (inside the teaser when there is one), a beat after
    # the impact flash so Instagram's thumbnail is never a white frame
    after_fx = 0.25 if spec.look and (spec.look.flash or spec.look.rgb_split) else 0.0
    if teaser_len:
        cover = min(spec.peak - max(0.0, spec.peak - TEASER_PRE) + after_fx, teaser_len - 0.1)
    else:
        cover = _stretch(p, slow) + after_fx
    return {"duration": round(final.duration, 2), "cover_ms": int(max(0, cover) * 1000), "style": spec.style,
            "fx": spec.look.to_dict() if spec.look else None, "encoder": media.video_encoder(spec.encoder)}

