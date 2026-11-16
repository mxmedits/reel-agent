"""Recording in → finished, captioned reels in the queue."""
from __future__ import annotations

import json
import shutil
import time
from datetime import date, datetime
from pathlib import Path

from . import captions, editor, highlights, looks, media, scout, transcribe, voiceover
from . import music as mus
from .db import DB

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts", ".flv"}


def log(msg: str, cfg=None):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if cfg is not None:
        try:
            with open(cfg.state_dir / "agent.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def released(profile: dict, on: date | None = None) -> bool:
    rel = profile.get("released")
    if not rel:
        return True
    return (on or date.today()) >= date.fromisoformat(str(rel))


def new_recordings(cfg, db: DB, settle_seconds: int = 60) -> list[Path]:
    """Video files in the inbox that are finished writing and not yet processed."""
    out = []
    for p in sorted(cfg.inbox.rglob("*")):
        if p.suffix.lower() not in VIDEO_EXT or not p.is_file() or p.name.startswith("."):
            continue
        if db.source_seen(p.resolve()):
            continue
        if time.time() - p.stat().st_mtime < settle_seconds:   # still recording / copying
            continue
        out.append(p)
    return out


def process_file(cfg, db: DB, path: Path, game: str | None = None) -> list[str]:
    path = path.resolve()
    media.require_ffmpeg()
    info = media.probe(path)
    game = game or cfg.detect_game(path)
    if game == "default" and cfg.ai_available:      # shared from Photos as IMG_1234.MOV: let the AI look
        seen = captions.detect_game(cfg, path, info)
        if seen:
            game = seen
            log(f"  👀 recognised the game: {cfg.game_profile(seen).get('name', seen)}", cfg)
    profile = scout.enrich_profile(cfg, cfg.game_profile(game))
    game = profile["key"]
    c, rv = cfg["clips"], cfg["review"]
    log(f"▶ {path.name}  [{profile['name']}]  {info.duration / 60:.1f} min, "
        f"{info.width}x{info.height}{'' if info.has_audio else ', no audio'}", cfg)

    rec_day = datetime.fromtimestamp(path.stat().st_mtime).date()
    if not released(profile, rec_day):
        log(f"  ✋ recorded {rec_day} before {profile['name']} launch ({profile['released']}): "
            "pre-release footage gets taken down. Skipping.", cfg)
        db.mark_source(path, "skipped", "pre-release footage")
        return []

    length = float(c["length"])
    want = int(c["per_video"])
    moments = highlights.find_moments(path, info, length, count=max(want * 2, want + 2),
                                      min_gap=float(c["min_gap"]))
    log(f"  found {len(moments)} candidate moments", cfg)

    candidates = []
    for i, m in enumerate(moments, 1):
        words = []
        if c.get("subtitles") and transcribe.available():
            try:
                words = transcribe.words(path, m.start, m.end - m.start, c.get("whisper_model", "small"))
            except Exception as e:
                log(f"  ! transcription failed: {e}", cfg)
        transcript = " ".join(w["w"] for w in words)
        copy = captions.write_copy(cfg, path, m, profile, transcript, db.peek_series(game) + len(candidates))
        verdict = f"quality {copy['quality']}/10"
        if copy.get("what_happens"):
            verdict += f" — {copy['what_happens']}"
        if copy["quality"] < int(rv["min_quality"]):
            log(f"  ✗ moment {i} @ {m.peak:.0f}s skipped: {verdict}", cfg)
            continue
        if rv.get("skip_spoilers", True) and copy.get("is_cutscene_or_spoiler"):
            log(f"  ✗ moment {i} @ {m.peak:.0f}s skipped: cutscene/spoiler", cfg)
            continue
        log(f"  ✓ moment {i} @ {m.peak:.0f}s {verdict}", cfg)
        candidates.append((m, copy, words))

    candidates.sort(key=lambda x: (x[1]["quality"], x[0].score), reverse=True)
    mus.ensure_library(cfg)
    mcfg, vcfg = cfg.get("music", {}) or {}, cfg.get("voiceover", {}) or {}
    recent_tracks = [json.loads(r["render_json"] or "{}").get("music") for r in db.clips()[-6:]]
    made = []
    for m, copy, words in candidates[:want]:
        n = db.next_series(game)
        clip_id = f"{game}-{n:04d}"
        folder = cfg.output / "clips" / clip_id
        folder.mkdir(parents=True, exist_ok=True)
        teaser = bool(c.get("teaser_hook", True)) and not copy.get("graphic_violence")
        payoff_in_video = (editor.TEASER_PRE + editor.TEASER_POST if teaser and m.peak - m.start > 4 else 0) \
            + (m.peak - m.start)

        # edit style: Claude picks per clip using the trend brief (or a fixed style from config)
        allowed = c.get("styles") or ["classic", "slowmo", "meme_bar"]
        style = c.get("style", "auto")
        if style == "auto":
            style = copy.get("edit_style") if copy.get("edit_style") in allowed else "classic"

        # music: a trending Instagram sound is attached at posting time when that's on; otherwise the
        # library track for Claude's mood, with its drop lined up on the payoff
        ig_sound = bool((cfg.get("instagram") or {}).get("trending_audio", True) and cfg.ig_user_id and cfg.ig_token)
        track = None if ig_sound else mus.pick_track(cfg, game, copy.get("music_mood"), recent_tracks)
        seek, delay = (mus.alignment(track, payoff_in_video)
                       if track and mcfg.get("sync_drop", True) else (0.0, 0.0))
        if track:
            recent_tracks.append(str(track))

        # voiceover: when there's no commentary and no music (auto), or always
        voice_wav = None
        transcript = " ".join(w["w"] for w in words)
        mode = vcfg.get("mode", "auto")
        if voiceover.wanted(cfg, transcript) and (mode == "always" or not (track or ig_sound)):
            ready, why = voiceover.engine_ready(cfg)
            if not ready:
                log(f"  (voiceover skipped: {why})", cfg)
            else:
                try:
                    lines = voiceover.write_script(cfg, path, m, copy, profile)
                    vwords = voiceover.build(cfg, lines, m.end - m.start, folder / "voiceover.wav")
                    if vwords:
                        voice_wav = folder / "voiceover.wav"
                        words = words or vwords
                        copy["voiceover"] = lines
                        if vcfg.get("disclose_ai_voice", True):
                            copy["instagram_caption"] = copy["instagram_caption"].rstrip() + "\n\nAI voiceover"
                except Exception as e:
                    log(f"  ! voiceover failed ({e}); posting without it", cfg)

        try:
            stats = media.tone_stats(path, m.start, m.end - m.start)
        except Exception:
            stats = None
        look = looks.plan(cfg, game, stats, ai_pick=copy.get("look"), impact=copy.get("impact", True) is not False,
                          graphic=bool(copy.get("graphic_violence")))
        spec = editor.RenderSpec(
            source=path, info=info, start=m.start, end=m.end, peak=m.peak,
            layout=profile.get("layout", "blur"), hook=copy["hook_text"], handle=cfg.get("handle", ""),
            words=words, teaser=teaser,
            punch=bool(c.get("punch_zoom", True)), fps=int(c["fps"]), crf=int(c["crf"]),
            preset=c.get("preset", "veryfast"), font=c.get("font", "Anton"),
            music=track, music_volume=float(mcfg.get("volume", 0.55)),
            game_volume=float(mcfg.get("game_volume", 0.6)), music_seek=seek, music_delay=delay,
            voice=voice_wav, style=style, meme_line=str(copy.get("meme_line") or ""),
            look=look, encoder=c.get("encoder", "auto"),
        )
        t0 = time.time()
        result = editor.render(spec, folder / "reel.mp4")
        media.grab_frame(folder / "reel.mp4", result["cover_ms"] / 1000, folder / "cover.jpg", width=1080)
        (folder / "instagram_caption.txt").write_text(captions.instagram_text(copy), encoding="utf-8")
        render_meta = {**result, "layout": spec.layout, "teaser": spec.teaser, "words": words,
                       "music": str(track) if track else None, "music_seek": seek, "music_delay": delay,
                       "music_mood": copy.get("music_mood"), "voice": str(voice_wav) if voice_wav else None,
                       "ig_sound_planned": ig_sound}
        (folder / "meta.json").write_text(json.dumps(
            {"id": clip_id, "source": str(path), "moment": m.to_dict(), "copy": copy, "render": render_meta},
            indent=2, ensure_ascii=False), encoding="utf-8")
        status = "ready" if rv.get("require_approval") else "queued"
        db.add_clip(id=clip_id, source=str(path), game=game, series_no=n, start=m.start, end=m.end,
                    peak=m.peak, score=m.score, quality=copy["quality"], status=status,
                    file=str(folder / "reel.mp4"), copy_json=copy, render_json=render_meta,
                    ig_status="pending")
        extras = [result.get("style", "classic"), f"look: {look.name}"]
        if ig_sound:
            extras.append("Instagram sound at posting")
        if track:
            extras.append(f"music: {copy.get('music_mood')}/{Path(track).name}")
        if voice_wav:
            extras.append("voiceover")
        log(f"  🎬 {clip_id}: \"{copy['hook_text']}\" (hook {copy.get('hook_score')}, "
            f"{result['duration']:.0f}s{', ' + ', '.join(extras) if extras else ''}, "
            f"rendered in {time.time() - t0:.0f}s) → {status}", cfg)
        made.append(clip_id)

    db.mark_source(path, "done", f"{len(made)} clips")
    if not made:
        log("  no moment passed the quality bar in this recording", cfg)
    if c.get("delete_recordings") and path.is_relative_to(cfg.inbox):
        path.unlink(missing_ok=True)      # reels are made; free the disk (hook re-renders need the original)
        log(f"  🧹 deleted original {path.name}", cfg)
    return made


def rerender(cfg, db: DB, clip_id: str, hook: str | None = None) -> None:
    row = db.get(clip_id)
    if not row:
        raise SystemExit(f"No clip {clip_id}")
    from .db import copy_of, render_of

    copy, rmeta = copy_of(row), render_of(row)
    if hook:
        copy["hook_text"] = hook
        if rmeta.get("style") == "meme_bar":
            copy["meme_line"] = hook
    src = Path(row["source"])
    if not src.exists():
        raise RuntimeError("the original recording was deleted, so the hook can't be re-rendered "
                           "(turn off 'delete recordings' in Settings to keep originals)")
    info = media.probe(src)
    c = cfg["clips"]
    spec = editor.RenderSpec(source=src, info=info, start=row["start"], end=row["end"], peak=row["peak"],
                             layout=rmeta.get("layout", "blur"), hook=copy["hook_text"],
                             handle=cfg.get("handle", ""), words=rmeta.get("words", []),
                             teaser=rmeta.get("teaser", True), punch=bool(c.get("punch_zoom", True)),
                             fps=int(c["fps"]), crf=int(c["crf"]), preset=c.get("preset", "veryfast"),
                             font=c.get("font", "Anton"),
                             music=Path(rmeta["music"]) if rmeta.get("music") else None,
                             music_volume=float((cfg.get("music") or {}).get("volume", 0.55)),
                             game_volume=float((cfg.get("music") or {}).get("game_volume", 0.6)),
                             music_seek=float(rmeta.get("music_seek", 0)),
                             music_delay=float(rmeta.get("music_delay", 0)),
                             voice=Path(rmeta["voice"]) if rmeta.get("voice") else None,
                             style=rmeta.get("style", "classic"), meme_line=str(copy.get("meme_line") or ""),
                             look=looks.plan(cfg, row["game"], media.tone_stats(src, row["start"], row["end"] - row["start"]),
                                             ai_pick=(rmeta.get("fx") or {}).get("look") or copy.get("look"),
                                             impact=copy.get("impact", True) is not False,
                                             graphic=bool(copy.get("graphic_violence"))),
                             encoder=c.get("encoder", "auto"))
    out = Path(row["file"])
    result = editor.render(spec, out)
    rmeta.update(result)
    db.update(clip_id, copy_json=copy, render_json=rmeta)
    log(f"re-rendered {clip_id} with hook \"{copy['hook_text']}\"", cfg)


def remove_clip_files(row) -> None:
    folder = Path(row["file"]).parent
    if folder.exists() and folder.name == row["id"]:
        shutil.rmtree(folder, ignore_errors=True)
