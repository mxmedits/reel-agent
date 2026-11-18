"""Recording in → finished, captioned reels in the queue."""
from __future__ import annotations

import json
import shutil
import time
from datetime import date, datetime
from pathlib import Path

from . import beats, captions, editor, formats, highlights, looks, media, reach, scout, screen, transcribe, voiceover
from . import music as mus
from .db import DB, copy_of

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts", ".flv"}


def log(msg: str, cfg=None):
    from .safety import scrub
    line = scrub(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
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


def see(cfg, path: Path, info) -> None:
    """Check whether the clip was filmed off a TV with a phone, and remember it for every later step."""
    try:
        tr = screen.analyse(path, info, cfg)
        info.screen = tr if tr is not None and tr.filmed else None
    except Exception as e:                                   # never let the check stop an edit
        log(f"  (screen check skipped: {e})", cfg)
        info.screen = None
    captions._VIEW[str(path)] = info


def recent_hooks(db: DB, n: int = 12) -> list[str]:
    return [copy_of(r).get("hook_text", "") for r in db.clips()[-n:]]


def process_file(cfg, db: DB, path: Path, game: str | None = None, plan: dict | None = None) -> list[str]:
    """Edit one recording into reels. `plan` is what the owner asked for when adding it (request.py)."""
    from . import request as rq
    plan = rq.clean(plan)
    path = path.resolve()
    media.require_ffmpeg()
    info = media.probe(path)
    see(cfg, path, info)
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
        f"{info.width}x{info.height}{'' if info.has_audio else ', no audio'}"
        + (", HDR" if info.hdr else ""), cfg)
    if info.screen is not None:
        log(f"  📺 filmed off a screen ({'; '.join(info.screen.notes)}): straightening it and cropping the room "
            f"away, {info.view_w}x{info.view_h}", cfg)

    rec_day = datetime.fromtimestamp(path.stat().st_mtime).date()
    if not released(profile, rec_day):
        log(f"  ✋ recorded {rec_day} before {profile['name']} launch ({profile['released']}): "
            "pre-release footage gets taken down. Skipping.", cfg)
        db.mark_source(path, "skipped", "pre-release footage")
        return []

    kind = plan["kind"]
    if not rq.is_default(plan):
        log(f"  📝 you asked for: {rq.label(plan) or rq.NAMES[kind]}" + (f"; note: \"{plan['note']}\"" if plan["note"] else ""), cfg)
    if kind in ("highlight", "full"):
        made = _long_form(cfg, db, path, info, profile, game, plan)
        db.mark_source(path, "done", f"{len(made)} clips")
        return made

    length = formats.reel_length(cfg, game)
    want = plan["count"] if kind == "shorts" else int(c["per_video"])
    if kind in ("top3", "montage"):
        want = editor.COMPILATIONS[kind] + 2
    # with an AI to judge them, offer it more moments so it can turn the weak ones down
    moments = highlights.find_moments(path, info, length, count=max(want * (3 if cfg.ai_available else 2), want + 2),
                                      min_gap=float(c["min_gap"]))
    named = []                                    # times the owner named in the note come first, and stay in
    for t in rq.times(plan["note"]):
        m = highlights.near(path, info, t, length)
        if m and all(min(m.end, x.end) - max(m.start, x.start) < 0.35 * length for x in named):
            named.append(m)
    if named:
        moments = named + [m for m in moments
                           if all(min(m.end, x.end) - max(m.start, x.start) < 0.35 * length for x in named)]
        log(f"  🎯 kept the moment{'s' if len(named) > 1 else ''} you named at "
            + ", ".join(f"{int(x.peak // 60)}:{int(x.peak % 60):02d}" for x in named), cfg)
    log(f"  found {len(moments)} candidate moments" + (f" (reels ~{length:.0f}s, from what top reels run at)"
                                                        if abs(length - float(c["length"])) >= 1 else ""), cfg)

    candidates = []
    for i, m in enumerate(moments, 1):
        words = []
        if c.get("subtitles") and transcribe.available():
            try:
                words = transcribe.words(path, m.start, m.end - m.start, c.get("whisper_model", "small"))
            except Exception as e:
                log(f"  ! transcription failed: {e}", cfg)
        transcript = " ".join(w["w"] for w in words)
        copy = captions.write_copy(cfg, path, m, profile, transcript, db.peek_series(game) + len(candidates),
                                   brief=rq.brief(plan),
                                   avoid=recent_hooks(db) + [x[1].get("hook_text", "") for x in candidates])
        verdict = f"quality {copy['quality']}/10"
        if copy.get("what_happens"):
            verdict += f" — {copy['what_happens']}"
        if m in named:
            copy["quality"] = max(copy["quality"], int(rv["min_quality"]))     # you asked for this one
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
    ctx = {"recent_tracks": [json.loads(r["render_json"] or "{}").get("music") for r in db.clips()[-6:]],
           "used_styles": []}
    made = []

    # a long recording with plenty of moments also gets one compilation, in place of the weakest single reel
    clean = [x for x in candidates if not x[1].get("graphic_violence")]
    if kind in ("top3", "montage"):
        if len(clean) >= editor.COMPILATIONS[kind]:
            comp, singles = kind, []
        else:
            log(f"  ! only {len(clean)} strong moment(s), not enough for {rq.NAMES[kind]}: making a single reel", cfg)
            comp, singles = None, candidates[:1]
    elif kind == "shorts":
        comp, singles = None, candidates[:want]
    else:
        comp = formats.compilation(cfg, db, game, len(clean), info.duration, want, seed=f"{path.name}-{len(candidates)}")
        singles = candidates[:max(want - 1, 1)] if comp else candidates[:want]

    for m, copy, words in singles:
        style, why = formats.choose(cfg, db, game, copy, ctx["used_styles"],
                                    seed=f"{game}-{db.peek_series(game)}-{m.peak:.1f}")
        clip_id = _produce(cfg, db, path, info, profile, game, m, copy, words, style, why, [], ctx, plan=plan)
        if clip_id:
            made.append(clip_id)

    if comp:
        try:
            picks = clean[:6 if comp == "montage" else 3]
            best_m, best_copy, _ = picks[0]
            name = profile.get("name", "Gaming")
            n = len(picks)
            if comp == "top3":
                brief = ("This reel is a TOP 3 COUNTDOWN of the 3 best moments of the session (#3, #2, #1); the stills "
                         "show the #1 moment. Write the hooks and the caption for the countdown as a whole, and ask "
                         "which of the three wins.")
                tmpl_hooks = [f"TOP 3 {name.upper()} MOMENTS", "3 CLIPS, 1 WINNER", "RANKING MY BEST 3 CLIPS"]
                tmpl_cap = f"Top 3 {name} moments from one session. Which one takes it: 1, 2 or 3?"
                card = "Which one wins?"
            else:
                brief = (f"This reel is a fast MONTAGE of the {n} best moments of the session; the stills show the best "
                         f"one. Write the hooks and the caption for the montage as a whole.")
                tmpl_hooks = [f"{name.upper()} BEST BITS", "ONE SESSION, ALL THE CHAOS", "BEST OF TODAY"]
                tmpl_cap = f"{name} best bits from one session. Send this to the friend you'd run it back with."
                card = ""
            if cfg.ai_available:
                copy = captions.write_copy(cfg, path, best_m, profile, "", db.peek_series(game), brief=brief)
            else:
                copy = captions._finish({**best_copy, "hook_options": tmpl_hooks, "instagram_caption": tmpl_cap}, profile)
            copy["quality"] = max(copy.get("quality", 0), best_copy.get("quality", 0))
            copy["end_card"] = copy.get("end_card") or card
            extra = [x[0].to_dict() for x in picks]
            clip_id = _produce(cfg, db, path, info, profile, game, best_m, copy, [], comp,
                               "you asked for it" if kind == comp else "plenty of strong moments in this recording",
                               extra, ctx, plan=plan)
            if clip_id:
                made.append(clip_id)
        except Exception as e:
            log(f"  ! {comp} compilation failed ({e}); the single reels are unaffected", cfg)

    for cid in made:
        _tell(cfg, db, cid, plan)
    db.mark_source(path, "done", f"{len(made)} clips")
    if not made:
        log("  no moment passed the quality bar in this recording", cfg)
    if c.get("delete_recordings") and path.is_relative_to(cfg.inbox):
        path.unlink(missing_ok=True)      # reels are made; free the disk (hook re-renders need the original)
        log(f"  🧹 deleted original {path.name}", cfg)
    return made


def _long_form(cfg, db: DB, path: Path, info, profile: dict, game: str, plan: dict) -> list[str]:
    """One video from the whole recording: the critical parts cut together (highlight), or the clip itself
    with a light edit (full)."""
    import math
    from . import request as rq
    dur, kind = info.duration, plan["kind"]
    must = rq.times(plan["note"])
    ctx = {"recent_tracks": [json.loads(r["render_json"] or "{}").get("music") for r in db.clips()[-6:]],
           "used_styles": []}
    if kind == "highlight" and dur <= float(plan["length"]) + 3:
        log(f"  the clip is {rq.fmt_len(dur)}, no longer than the {rq.fmt_len(plan['length'])} you asked for: "
            "posting all of it instead of cutting it", cfg)
        kind = "full"
        plan = {**plan, "kind": "full", "length": plan["length"]}
    if kind == "full":
        limit = rq.API_MAX if plan["length"] == "all" else float(plan["length"])
        m = highlights.best_window(path, info, min(limit, dur), must)
        style, extra = "classic", []
        what = rq.fmt_len(m.end - m.start)
        if dur > limit + 1:
            log(f"  ✂ the clip is {rq.fmt_len(dur)}: keeping the best {rq.fmt_len(limit)} "
                f"({int(m.start // 60)}:{int(m.start % 60):02d} to {int(m.end // 60)}:{int(m.end % 60):02d})", cfg)
        why = "the full video, as you asked"
    else:
        target = float(plan["length"])
        seg = max(5.0, min(15.0, target / 6))
        n = max(2, math.ceil(target / seg))
        found = highlights.find_moments(path, info, seg, count=n + 4, min_gap=seg * 0.8)
        named = [x for x in (highlights.near(path, info, t, seg) for t in must) if x]
        picks = list(named)
        for x in found:
            if len(picks) >= n:
                break
            if all(min(x.end, y.end) - max(x.start, y.start) < 0.3 * seg for y in picks):
                picks.append(x)
        if len(picks) < 2:
            log("  ! not enough distinct moments for a highlight video: posting the best stretch instead", cfg)
            return _long_form(cfg, db, path, info, profile, game, {**plan, "kind": "full", "length": target})
        m = max(picks, key=lambda x: x.score)
        style, extra = "highlight", [x.to_dict() for x in picks]
        what = f"{len(picks)} moments, about {rq.fmt_len(sum(x.end - x.start for x in picks))}"
        why = "the critical parts, as you asked"
        log(f"  🎞 highlight video: {what}, in the order they happened", cfg)
    copy = captions.write_copy(cfg, path, m, profile, "", db.peek_series(game),
                               brief=rq.brief(plan, what), avoid=recent_hooks(db))
    copy["quality"] = max(int(copy.get("quality") or 0), int(cfg["review"]["min_quality"]))
    clip_id = _produce(cfg, db, path, info, profile, game, m, copy, [], style, why, extra, ctx, plan=plan)
    if clip_id:
        _tell(cfg, db, clip_id, plan)
    return [clip_id] if clip_id else []


def _tell(cfg, db: DB, clip_id: str, plan: dict) -> None:
    """When the owner asked for something particular, the reel's chat opens with it and with what was made."""
    from . import request as rq
    from .revise import add_chat
    if rq.is_default(plan):
        return
    row = db.get(clip_id)
    copy, r = copy_of(row), json.loads(row["render_json"] or "{}")
    asked = rq.label(plan) or rq.NAMES[plan["kind"]]
    add_chat(db, clip_id, "you", f"When adding the clip: {asked}" + (f". {plan['note']}" if plan["note"] else ""))
    kind = formats.ON_REQUEST.get("full" if plan["kind"] == "full" else r.get("style"), {}).get("name") \
        or formats.FORMATS.get(r.get("style", ""), {}).get("name", "reel")
    bits = [f"{kind}, {rq.fmt_len(r.get('duration') or 0)}"]
    if r.get("trimmed_from"):
        bits.append(f"the best part of your {rq.fmt_len(r['trimmed_from'])} clip")
    sound = (copy.get("ig_audio_plan") or {}).get("title")
    bits.append(f"Instagram song \"{sound}\" added when it's posted" if sound else
                "a beat made for it" if str(r.get("music") or "").startswith("beat:") else
                "your music" if r.get("music") else "game sound only")
    if not sound and not r.get("music") and plan["sound"] in ("music", "both"):
        bits[-1] += " (Instagram offered no song for it, so none is on the video)"
    if r.get("voice"):
        bits.append("AI voiceover")
    if (r.get("overrides") or {}).get("no_text"):
        bits.append("no title")
    msg = "Made it: " + "; ".join(bits) + "."
    if r.get("note_applied"):
        msg += " From your note: " + r["note_applied"] + "."
    if copy.get("song_ideas") and not sound:
        msg += " Songs that fit it are listed under the video."
    add_chat(db, clip_id, "agent", msg)


def _produce(cfg, db: DB, path: Path, info, profile: dict, game: str, m, copy: dict, words: list,
             style: str, why: str, extra_moments: list, ctx: dict, plan: dict | None = None) -> str | None:
    """Edit, save and queue one reel. Returns its id."""
    c, rv = cfg["clips"], cfg["review"]
    mcfg, vcfg = cfg.get("music", {}) or {}, cfg.get("voiceover", {}) or {}
    n = db.next_series(game)
    clip_id = f"{game}-{n:04d}"
    folder = cfg.output / "clips" / clip_id
    folder.mkdir(parents=True, exist_ok=True)
    from . import request as rq
    from . import trends
    plan = plan or rq.clean(None)
    o = rq.overrides(plan)                       # the owner's sound / voice / text choices + their note
    longform = plan["kind"] in ("highlight", "full")
    compilation = style in editor.COMPILATIONS
    teaser = bool(c.get("teaser_hook", True)) and not copy.get("graphic_violence") and plan["kind"] != "full"

    # sound: an Instagram song (attached when posted), else your own tracks, else (only if switched on) a beat
    want_music = o.get("music")                  # None = the agent's choice
    ig_plan = None
    if want_music is not False:
        ig_plan = reach.plan_sound(cfg, db, copy, game, ctx.setdefault("ig_sounds", []), log=lambda m: log(m, cfg))
        copy["ig_audio_options"] = reach.LAST.get("options") or []
    if ig_plan:
        copy["ig_audio_plan"] = ig_plan
        ctx["ig_sounds"].append(ig_plan["audio_id"])
    ig_sound = bool(ig_plan)
    track = None if (ig_sound or want_music is False) else mus.pick_track(cfg, game, copy.get("music_mood"), ctx["recent_tracks"])
    make_beat = (not ig_sound and track is None and want_music is not False
                 and (mcfg.get("generate", False) or o.get("beat_ok")))
    if not (ig_sound or track or make_beat) and o.get("game_audio") == "off":
        o.pop("game_audio")                      # no music came: muting the game would leave the reel silent
    copy["song_ideas"] = trends.songs_for(cfg, copy.get("music_mood"), seed=f"{game}-{db.peek_series(game)}")
    voice_wav = None
    transcript = " ".join(w["w"] for w in words)
    mode = "always" if o.get("voiceover") is True else vcfg.get("mode", "auto")
    allow_voice = o.get("voiceover") is not False and not longform
    if not compilation and allow_voice and (voiceover.wanted(cfg, transcript) or mode == "always") \
            and (mode == "always" or not (track or ig_sound or make_beat)):
        ready, reason = voiceover.engine_ready(cfg)
        if not ready:
            log(f"  (voiceover skipped: {reason})", cfg)
        else:
            try:
                lines = voiceover.write_script(cfg, path, m, copy, profile)
                vwords = voiceover.build(cfg, lines, m.end - m.start, folder / "voiceover.wav")
                if vwords:
                    voice_wav = folder / "voiceover.wav"
                    if c.get("subtitles", False):      # subtitles only when switched on in settings
                        words = words or vwords
                    copy["voiceover"] = lines
                    if vcfg.get("disclose_ai_voice", True):
                        copy["instagram_caption"] = copy["instagram_caption"].rstrip() + "\n\nAI voiceover"
            except Exception as e:
                log(f"  ! voiceover failed ({e}); posting without it", cfg)

    if copy.get("source") == "template":       # no AI: a hook that fits the kind of reel and isn't on a recent one
        hook = captions.template_hook(profile, style, recent_hooks(db), n)
        copy["hook_text"], copy["hook_options"] = hook, [hook]
    try:
        stats = media.tone_stats(path, m.start, m.end - m.start, pre=info.pre(m.start, m.end))
        color = media.color_stats(path, m.start, m.end - m.start, pre=info.pre(m.start, m.end)) \
            if info.screen is not None else None
    except Exception:
        stats = color = None
    look = looks.plan(cfg, game, stats, ai_pick=copy.get("look"), impact=copy.get("impact", True) is not False,
                      graphic=bool(copy.get("graphic_violence")), filmed=info.screen is not None, color=color)
    looks.apply_overrides(look, o)
    card = formats.end_card(cfg, game, copy, profile, seed=clip_id) if not voice_wav else ""
    if "end_card" in o:
        card = o["end_card"]
    if not c.get("subtitles", False):
        words = []                            # no word-by-word subtitles on the video
    game_level = {"off": 0.0, "low": 0.25}.get(o.get("game_audio") or "")
    length = sum(float(x["end"]) - float(x["start"]) for x in extra_moments) if extra_moments else m.end - m.start
    spec = editor.RenderSpec(
        source=path, info=info, start=m.start, end=m.end, peak=m.peak,
        layout=o.get("layout") or profile.get("layout", "blur"),
        hook="" if o.get("no_text") else copy["hook_text"], handle=cfg.get("handle", ""),
        words=words, teaser=teaser,
        punch=bool(o.get("punch_zoom", c.get("punch_zoom", True))), fps=int(c["fps"]),
        crf=16 if o.get("quality") == "high" else int(c["crf"]),
        preset="fast" if o.get("quality") == "high" and length <= 90 else c.get("preset", "veryfast"),
        font=c.get("font", "Anton"),
        music_volume={"low": 0.35, "high": 0.8}.get(o.get("music_volume"), float(mcfg.get("volume", 0.55))),
        game_volume=float(mcfg.get("game_volume", 0.6)) if game_level is None else game_level,
        game_volume_alone=1.0 if game_level is None else game_level,
        voice=voice_wav, style=style, meme_line=str(copy.get("meme_line") or ""),
        look=look, encoder=c.get("encoder", "auto"),
        moments=extra_moments, freeze_line=str(copy.get("freeze_line") or ""), end_card=card,
        cut_seconds=formats.cut_seconds(cfg, game), text_scale=float(o.get("text_scale") or c.get("text_scale", 1.0)),
        maxrate_k=min(9000, int(280 * 8 * 1000 / length)) if length > 60 else 0,   # long videos stay under 300 MB
    )
    # music: a trending Instagram sound is attached at posting time when that's on; otherwise the library
    # track for the AI's mood, with its drop lined up on the payoff of the finished edit
    seek, delay = (mus.alignment(track, editor.payoff_time(spec))
                   if track and mcfg.get("sync_drop", True) else (0.0, 0.0))
    spec.music, spec.music_seek, spec.music_delay = track, seek, delay
    if track:
        ctx["recent_tracks"].append(str(track))
    beat_note = ""
    if make_beat:
        plan_ = editor.timeline(spec)
        mood = copy.get("music_mood") or profile.get("music_mood", "hype")
        spec.music = beats.make(mood, plan_["total"] + 0.5, plan_["payoff"], folder / "beat.wav", seed=n)
        beat_note = f"beat:{mood}"

    t0 = time.time()
    result = editor.render(spec, folder / "reel.mp4")
    ctx["used_styles"].append(result["style"])
    copy["edit_style"], copy["style_why"] = result["style"], (result.get("fallback") or why)
    media.grab_frame(folder / "reel.mp4", result["cover_ms"] / 1000, folder / "cover.jpg", width=1080)
    (folder / "instagram_caption.txt").write_text(captions.instagram_text(copy), encoding="utf-8")
    from .revise import describe
    note_kw = {k: v for k, v in rq.overrides({**rq.clean(None), "note": plan["note"]}).items()} if plan["note"] else {}
    render_meta = {**result, "layout": spec.layout, "teaser": spec.teaser, "words": words,
                   "music": str(track) if track else (beat_note or None), "music_seek": seek, "music_delay": delay,
                   "music_mood": copy.get("music_mood"), "voice": str(voice_wav) if voice_wav else None,
                   "ig_sound_planned": ig_sound, "moments": extra_moments, "end_card_text": card,
                   "overrides": o, "format": "full" if plan["kind"] == "full" else None,
                   "note_applied": describe(note_kw) if note_kw else "",
                   "trimmed_from": round(info.duration, 1) if plan["kind"] == "full" and info.duration > m.end - m.start + 1 else None}
    copy["owner_plan"] = plan
    (folder / "meta.json").write_text(json.dumps(
        {"id": clip_id, "source": str(path), "moment": m.to_dict(), "copy": copy, "render": render_meta},
        indent=2, ensure_ascii=False), encoding="utf-8")
    status = "ready" if rv.get("require_approval") else "queued"
    db.add_clip(id=clip_id, source=str(path), game=game, series_no=n, start=m.start, end=m.end,
                peak=m.peak, score=m.score, quality=copy["quality"], status=status,
                file=str(folder / "reel.mp4"), copy_json=copy, render_json=render_meta,
                ig_status="pending")
    kind = formats.FORMATS.get(result["style"], {}).get("name", result["style"])
    extras = [f"{kind} ({copy['style_why']})", f"look: {look.name}"]
    if card and result.get("end_card"):
        extras.append(f"ends on \"{card}\"")
    if ig_sound:
        extras.append(f"Instagram sound \"{ig_plan.get('title', '')}\" (added when posted)")
    if track:
        extras.append(f"music: {copy.get('music_mood')}/{Path(track).name}")
    elif beat_note:
        extras.append(f"{beat_note.split(':')[1]} beat made for it")
    if voice_wav:
        extras.append("voiceover")
    log(f"  🎬 {clip_id}: \"{copy['hook_text']}\" (hook {copy.get('hook_score')}, "
        f"{result['duration']:.0f}s, {', '.join(extras)}, rendered in {time.time() - t0:.0f}s) → {status}", cfg)
    return clip_id


def rerender(cfg, db: DB, clip_id: str, hook: str | None = None) -> None:
    row = db.get(clip_id)
    if not row:
        raise SystemExit(f"No clip {clip_id}")
    from .db import copy_of, render_of

    copy, rmeta = copy_of(row), render_of(row)
    if hook:
        copy["hook_text"] = hook
        if rmeta.get("style") in ("meme_bar", "pov"):
            copy["meme_line"] = hook
    src = Path(row["source"])
    if not src.exists():
        raise RuntimeError("the original recording was deleted, so the hook can't be re-rendered "
                           "(turn off 'delete recordings' in Settings to keep originals)")
    info = media.probe(src)
    see(cfg, src, info)
    c = cfg["clips"]
    spec = editor.RenderSpec(source=src, info=info, start=row["start"], end=row["end"], peak=row["peak"],
                             layout=rmeta.get("layout", "blur"), hook=copy["hook_text"],
                             handle=cfg.get("handle", ""), words=rmeta.get("words", []) if c.get("subtitles", False) else [],
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
                             moments=rmeta.get("moments") or [], freeze_line=str(copy.get("freeze_line") or ""),
                             end_card=str(rmeta.get("end_card_text") or ""),
                             look=looks.plan(cfg, row["game"], media.tone_stats(src, row["start"], row["end"] - row["start"],
                                                                                pre=info.pre(row["start"], row["end"])),
                                             filmed=info.screen is not None,
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
