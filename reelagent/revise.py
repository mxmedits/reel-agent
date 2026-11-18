"""The chat box: the owner says what's wrong with a reel, the agent re-edits it and asks again.

  "the text is too small and the clip starts too late"  →  an edit plan  →  a new version of the reel
  →  back to "waiting for your OK" with a short reply saying what was changed.

With an AI key the note is read by the AI, which turns it into a plan (and spots wishes meant for every
future reel, which are saved). Without one, a keyword reader handles the common requests.
Per-reel changes are kept with the reel, so a second note builds on the first.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import beats, captions, editor, formats, highlights, looks, media, pipeline, reach, voiceover
from . import music as mus
from .db import DB, copy_of, render_of

STYLES = list(editor.STYLES)
LOOKS = [k for k in looks.LOOKS]
LAYOUTS = ["blur", "zoom", "fit", "crop"]
MOODS = ["hype", "intense", "epic", "chill", "funny", "suspense"]
FX_SWITCHES = ["shake", "flash", "rgb_split", "cut_flash", "grain", "glow", "vignette", "sharpen", "punch_zoom", "teaser"]

PLAN_PROMPT = """You are the video editor of a gaming Instagram page. The page owner watched a reel you made and \
wrote a note. Turn the note into settings for the next version of THIS reel. Set everything the note asks for, \
including things asked in earlier notes that the owner says still aren't done. Leave the rest null.

The reel now:
{facts}

Conversation so far about this reel (oldest first):
{chat}

Settings (null = leave as it is):
- hook: the title shown at the start (4-7 words, plain words)
- caption: the Instagram caption text (no hashtags in it); hashtags: list of at most 5, each starting with #
- style: the kind of reel: {styles}
- look: colour grade: {looks}; look_strength: 0.5 (subtle) to 1.5 (bold)
- brightness, contrast, saturation: -1 (much less) to 1 (much more)
- quality: "high" when the owner wants a sharper, clearer, better-quality picture
- layout: blur (whole game picture on a blurred background), zoom (fill more of the screen), fit (black bars), crop (fill all)
- length: seconds, 10 to 45; shift: seconds to move the clip (negative = start earlier)
- different_moment: true for a different part of the gameplay
- end_card: the question on the last second, "" to remove it; text_scale: 0.7 to 1.5 for all on-screen text
- voiceover: false to remove the AI voice, true to add one
- game_audio: "off" (no game sound at all), "low" or "normal"
- music: true to add music, false for none; music_mood: {moods}; music_volume: "low", "normal" or "high"
- music_source: "instagram" for a song from Instagram's music library, "agent" for a beat the agent makes
- new_song: true when the owner wants a different song
- switches true/false: shake, flash, rgb_split, cut_flash, grain, glow, vignette, sharpen, punch_zoom, teaser
- remember: true only for wishes about EVERY future reel ("always", "never", "from now on")
- note_for_future: that wish in one short sentence, when remember is true
- cannot: what was asked that no setting above can do, else ""

Examples:
"remove the voiceover" -> {{"voiceover": false}}
"no game sound, add music that fits" -> {{"game_audio": "off", "music": true}}
"music isn't added" -> {{"music": true}}
"pick music from instagram" -> {{"music": true, "music_source": "instagram"}}
"I don't like this song" -> {{"music": true, "new_song": true}}
"I can't hear the music, mute the game" -> {{"game_audio": "off", "music": true}}
"make it look better quality" -> {{"quality": "high"}}
"the title is too small and it starts too late" -> {{"text_scale": 1.25, "shift": -4}}
"never use the white flash" -> {{"flash": false, "cut_flash": false, "remember": true, "note_for_future": "No white flashes."}}

Reply ONLY with one JSON object containing the settings you set."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_chat(db: DB, clip_id: str, who: str, text: str) -> None:
    row = db.get(clip_id)
    copy = copy_of(row)
    chat = list(copy.get("chat") or [])
    chat.append({"from": who, "text": str(text)[:1500], "at": _now()})
    copy["chat"] = chat[-40:]
    db.update(clip_id, copy_json=copy)


def facts(row) -> str:
    c, r = copy_of(row), render_of(row)
    o = r.get("overrides") or {}
    fx = r.get("fx") or {}
    return "\n".join([
        f"- game: {row['game']}; length {r.get('duration')}s; kind of reel: {r.get('style')}; layout: {r.get('layout')}",
        f"- title: \"{c.get('hook_text', '')}\"; end question: \"{r.get('end_card_text') or ''}\"",
        f"- caption: \"{c.get('instagram_caption', '')}\"; hashtags: {' '.join(c.get('hashtags', []))}",
        f"- colour grade: {fx.get('look')}; effects on the big moment: {', '.join(fx.get('impact') or []) or 'none'}; "
        f"grain {fx.get('grain')}, glow {fx.get('glow')}, vignette {fx.get('vignette')}",
        f"- sound: music {r.get('music') or 'none'}; AI voiceover {'yes' if r.get('voice') else 'no'}; "
        f"game sound {o.get('game_audio') or 'normal'}",
        f"- changes the owner already asked for: {json.dumps(o) if o else 'none'}",
    ])


# ── reading the note ─────────────────────────────────────────────────────────

def _num(x, lo, hi):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def clean_plan(d: dict) -> dict:
    """Keep only valid values."""
    p: dict = {"reply": str(d.get("reply") or "").strip()[:400], "cannot": str(d.get("cannot") or "").strip()[:300]}
    if isinstance(d.get("hook"), str) and d["hook"].strip():
        p["hook"] = " ".join(d["hook"].split()[:9]).upper()
    if isinstance(d.get("caption"), str) and d["caption"].strip():
        p["caption"] = d["caption"].strip()[:1800]
    if isinstance(d.get("hashtags"), list) and d["hashtags"]:
        p["hashtags"] = captions._clean_hashtags(d["hashtags"], [])
    if d.get("style") in STYLES:
        p["style"] = d["style"]
    if d.get("look") in LOOKS:
        p["look"] = d["look"]
    if d.get("layout") in LAYOUTS:
        p["layout"] = d["layout"]
    if d.get("music_mood") in MOODS:
        p["music_mood"] = d["music_mood"]
    if d.get("game_audio") in ("off", "low", "normal"):
        p["game_audio"] = d["game_audio"]
    if d.get("music_volume") in ("low", "normal", "high"):
        p["music_volume"] = d["music_volume"]
    if d.get("quality") == "high":
        p["quality"] = "high"
    if d.get("music_source") in ("instagram", "agent"):
        p["music_source"] = d["music_source"]
    if d.get("new_song") is True:
        p["new_song"] = True
    for k in ("voiceover", "music"):
        if isinstance(d.get(k), bool):
            p[k] = d[k]
    for k, lo, hi in (("look_strength", 0.3, 1.6), ("brightness", -1, 1), ("contrast", -1, 1), ("saturation", -1, 1),
                      ("length", 8, 45), ("shift", -30, 30), ("text_scale", 0.6, 1.6)):
        v = _num(d.get(k), lo, hi)
        if v is not None and not (k in ("brightness", "contrast", "saturation", "shift") and abs(v) < 1e-6):
            p[k] = v
    if isinstance(d.get("end_card"), str):
        p["end_card"] = " ".join(d["end_card"].split()[:5])
    for k in FX_SWITCHES:
        if isinstance(d.get(k), bool):
            p[k] = d[k]
    if d.get("different_moment") is True:
        p["different_moment"] = True
    if d.get("remember") is True and isinstance(d.get("note_for_future"), str) and d["note_for_future"].strip():
        p["remember"] = True
        p["note_for_future"] = d["note_for_future"].strip()[:300]
    return p


RULES = [   # (pattern, plan changes): used without an AI key, and to catch anything the AI missed
    (r"\b(no|remove|without|mute|drop|cut|delete|turn off|get rid of)\b[^.]*\b(game|background|original|gameplay) (voice|sound|audio|noise)", {"game_audio": "off"}),
    (r"\b(game|background|original|gameplay) (voice|sound|audio|noise)\b[^.]*\b(off|removed?|muted?|too loud|lower)\b", {"game_audio": "off"}),
    (r"\b(no|remove|without|drop|delete|turn off|get rid of)\b[^.]*\b(voice ?over|narrat\w*|ai voice|robot voice)\b", {"voiceover": False}),
    (r"\b(voice ?over|narrat\w*|ai voice)\b[^.]*\b(not removed|still there|still in|remove|annoying)\b", {"voiceover": False}),
    (r"\binstagram\b[^.]*\b(music|song|sound|audio|track)\b|\b(music|song|sound|audio|track)\b[^.]*\b(from|on|of) (instagram|insta|ig)\b", {"music": True, "music_source": "instagram"}),
    (r"\b(different|another|other|new|change the|change) (song|music|track|sound)\b|\bdon'?t like (the|this) (song|music|track)\b", {"music": True, "new_song": True}),
    (r"\b(can'?t|cannot|can not) hear (the )?(music|song)\b", {"music": True, "game_audio": "off"}),
    (r"\b(add|put|use|with|need|want|include)\b[^.]*\b(music|song|beat|track|soundtrack)\b", {"music": True}),
    (r"\b(music|song|beat)\b[^.]*\b(not added|missing|isn'?t (there|added)|is not)\b|\bno music (was|is) added\b", {"music": True}),
    (r"\b(no|remove|without|stop)\b[^.]*\b(music|song|beat)\b", {"music": False}),
    (r"\b(quality|sharper|sharp|blurry|blurred|hd|clearer|crisp|pixelated|low res|grainy)\b", {"quality": "high"}),
    (r"\b(music|song|beat) (is |was |a bit |way )*(too loud|very loud|loud)\b|\b(lower|quieter|softer) (the )?(music|song)\b", {"music_volume": "low"}),
    (r"\b(music|song|beat) (is |was |a bit |way )*(too quiet|too low|too soft|not loud)\b|\b(louder) (the )?(music|song)\b|\b(music|song) louder\b", {"music_volume": "high"}),
    (r"\b(bigger|larger|huge|can'?t read|too small)\b.*\b(text|title|font|words?)\b|\b(text|title|font)\b.*\b(bigger|larger|too small)\b", {"text_scale": 1.25}),
    (r"\b(smaller|too big|too large)\b.*\b(text|title|font)\b|\b(text|title|font)\b.*\b(smaller|too big|too large)\b", {"text_scale": 0.8}),
    (r"\b(no|remove|without|drop|get rid of)\b.*\b(shake|shaking)\b|\btoo much shake\b", {"shake": False}),
    (r"\b(no|remove|without|drop)\b.*\b(flash|white flash)\b", {"flash": False, "cut_flash": False}),
    (r"\b(no|remove|without|drop)\b.*\b(effects?|fx)\b", {"shake": False, "flash": False, "rgb_split": False, "cut_flash": False}),
    (r"\b(no|remove|without|drop)\b.*\b(grain|noise)\b", {"grain": False}),
    (r"\b(no|remove|without|drop)\b.*\b(question|end card|rate this)\b", {"end_card": ""}),
    (r"\b(no|remove|without|drop)\b.*\b(intro|teaser|preview)\b", {"teaser": False}),
    (r"\b(no|remove|without|drop)\b.*\bzoom\b", {"punch_zoom": False}),
    (r"\b(too )?dark\b|\bbrighter\b", {"brightness": 0.4}),
    (r"\b(too )?bright\b|\bdarker\b|\bwashed\b", {"brightness": -0.3, "contrast": 0.3}),
    (r"\b(more colou?r|more vivid|dull|grey|gray|faded|colou?rless)\b", {"saturation": 0.4, "contrast": 0.2}),
    (r"\b(less colou?r|too colou?rful|oversaturated|too orange|too red)\b", {"saturation": -0.35}),
    (r"\b(shorter|too long|cut it down)\b", {"length": -1}),
    (r"\b(longer|too short|more of it)\b", {"length": +1}),
    (r"\b(starts? too late|start earlier|earlier|missed the start)\b", {"shift": -4}),
    (r"\b(starts? too early|start later|boring start|later)\b", {"shift": 4}),
    (r"\b(different|another|other|better) (moment|part|clip|bit)\b|\bboring\b|\bnothing happens\b|\bwrong moment\b", {"different_moment": True}),
    (r"\bslow ?mo(tion)?\b", {"style": "slowmo"}),
    (r"\breplay\b", {"style": "replay"}),
    (r"\bfreeze\b", {"style": "freeze"}),
    (r"\bmontage\b", {"style": "montage"}),
    (r"\b(top ?3|countdown)\b", {"style": "top3"}),
    (r"\b(fill|full ?screen|bigger (video|picture|game)|zoom in)\b", {"layout": "zoom"}),
    (r"\b(whole|full) (picture|game|screen)\b|\btoo zoomed\b", {"layout": "blur"}),
]
FUTURE = re.compile(r"\b(always|never|from now on|every (reel|video|clip)|all (reels|videos|clips)|in general|in future|going forward)\b", re.I)


def plan_from_keywords(text: str, row) -> dict:
    t = text.lower()
    plan: dict = {}
    for pat, change in RULES:
        if re.search(pat, t):
            for k, v in change.items():
                plan.setdefault(k, v)
    if plan.get("music") is True and plan.get("game_audio") is None and re.search(r"\binstead\b", t):
        plan["game_audio"] = "low"
    if "length" in plan:                    # relative: ±25 %
        cur = float(render_of(row).get("duration") or 20)
        plan["length"] = max(8.0, min(45.0, cur * (0.75 if plan["length"] < 0 else 1.3)))
    m = re.search(r"(?:title|hook|text)\s*(?:to|:|should say|say)\s*[\"“']?([^\"”'\n]{3,60})", text, re.I)
    if m:
        plan["hook"] = m.group(1).strip().upper()
    done = describe(plan)
    plan["reply"] = (f"Done: {done}." if done else
                     "I couldn't tell what to change from that without the AI key. Try a short note like "
                     "\"bigger text\", \"no shake\", \"start earlier\", \"brighter\" or \"a different moment\".")
    if done and FUTURE.search(text):
        plan["remember"], plan["note_for_future"] = True, text.strip()[:300]
    return plan


def describe(plan: dict) -> str:
    out = []
    names = {"text_scale": lambda v: "bigger text" if v > 1 else "smaller text",
             "brightness": lambda v: "brighter" if v > 0 else "darker",
             "contrast": lambda v: "more contrast" if v > 0 else "less contrast",
             "saturation": lambda v: "more colour" if v > 0 else "less colour",
             "length": lambda v: f"{v:.0f} seconds long",
             "shift": lambda v: f"starts {abs(v):.0f}s {'earlier' if v < 0 else 'later'}",
             "different_moment": lambda v: "a different moment from your clip",
             "style": lambda v: f"made as {formats.FORMATS.get(v, {}).get('name', v).lower()}",
             "look": lambda v: f"{v} colour grade", "layout": lambda v: f"{v} layout",
             "hook": lambda v: f"new title \"{v}\"", "caption": lambda v: "new caption",
             "hashtags": lambda v: "new hashtags", "end_card": lambda v: f"end question \"{v}\"" if v else "no end question",
             "music_mood": lambda v: f"{v} music",
             "voiceover": lambda v: "AI voiceover added" if v else "AI voiceover removed",
             "game_audio": lambda v: {"off": "game sound removed", "low": "game sound turned down", "normal": "game sound back to normal"}[v],
             "music": lambda v: "music added" if v else "music removed",
             "music_volume": lambda v: f"music {'quieter' if v == 'low' else 'louder' if v == 'high' else 'at normal level'}",
             "quality": lambda v: "sharper, cleaner picture at a higher quality setting",
             "music_source": lambda v: "music from Instagram" if v == "instagram" else "a beat made for the reel",
             "new_song": lambda v: "a different song"}
    for k, v in plan.items():
        if k in names:
            out.append(names[k](v))
        elif k in FX_SWITCHES:
            out.append(f"{k.replace('_', ' ')} {'on' if v else 'off'}")
    return ", ".join(out)


def read_note(cfg, row, text: str) -> dict:
    if not cfg.ai_available:
        return plan_from_keywords(text, row)
    chat = "\n".join(f"{m['from']}: {m['text']}" for m in (copy_of(row).get("chat") or [])[-10:]) or "(none)"
    prompt = PLAN_PROMPT.format(facts=facts(row), chat=chat, styles=", ".join(STYLES), looks=", ".join(LOOKS),
                                moods=", ".join(MOODS))
    kw = plan_from_keywords(text, row)
    try:
        d = captions.parse_json(captions.ask_claude(
            cfg, "You turn a video owner's notes into precise edit settings. JSON only.",
            [{"type": "text", "text": prompt + f"\n\nThe owner's new note: \"{text}\""}], 700))
        plan = clean_plan(d)
    except Exception as e:
        pipeline.log(f"  (AI couldn't read the note: {e}; using keywords)", cfg)
        return kw
    for k, v in kw.items():             # safety net: clear requests the AI left out
        if k not in ("reply",) and k not in plan:
            plan[k] = v
    return plan


# ── remembering wishes for future reels ─────────────────────────────────────

def notes_path(cfg) -> Path:
    return cfg.state_dir / "owner_notes.json"


def owner_notes(cfg) -> list[str]:
    try:
        return json.loads(notes_path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return []


def remember(cfg, plan: dict) -> None:
    notes = owner_notes(cfg)
    if plan.get("note_for_future") and plan["note_for_future"] not in notes:
        notes.append(plan["note_for_future"])
        notes_path(cfg).write_text(json.dumps(notes[-20:], indent=1, ensure_ascii=False), encoding="utf-8")
    # switches that map straight onto settings
    fx = {}
    for k, key in (("shake", "impact_shake"), ("flash", "impact_flash"), ("rgb_split", "rgb_split"),
                   ("cut_flash", "cut_flash"), ("grain", "grain"), ("glow", "glow"), ("vignette", "vignette"),
                   ("sharpen", "sharpen")):
        if k in plan:
            fx[key] = plan[k]
    clips = {}
    if fx:
        clips["fx"] = fx
    if "teaser" in plan:
        clips["teaser_hook"] = plan["teaser"]
    if "punch_zoom" in plan:
        clips["punch_zoom"] = plan["punch_zoom"]
    if plan.get("end_card") == "":
        clips["end_cards"] = False
    if "text_scale" in plan:
        clips["text_scale"] = plan["text_scale"]
    if clips:
        from .cloud import _merge
        cfg.save_user_config(_merge(cfg.user_config(), {"clips": clips}))


# ── re-making the reel ───────────────────────────────────────────────────────

def remake(cfg, db: DB, clip_id: str, plan: dict, src: Path) -> dict:
    """Re-edit one reel from its original recording with the plan applied. Returns the render result."""
    row = db.get(clip_id)
    copy, rmeta = copy_of(row), render_of(row)
    o = dict(rmeta.get("overrides") or {})
    for k, v in plan.items():
        if k in ("reply", "cannot", "remember", "note_for_future", "different_moment", "shift", "hook", "caption", "hashtags"):
            continue
        if k in ("brightness", "contrast", "saturation"):
            o[k] = max(-1.0, min(1.0, float(o.get(k, 0)) + float(v)))
        else:
            o[k] = v
    if plan.get("hook"):
        copy["hook_text"] = plan["hook"]
        if (o.get("style") or rmeta.get("style")) in ("meme_bar", "pov"):
            copy["meme_line"] = plan["hook"].capitalize()
    if plan.get("caption"):
        copy["instagram_caption"] = plan["caption"]
    if plan.get("hashtags"):
        copy["hashtags"] = plan["hashtags"]
    if plan.get("music_mood"):
        copy["music_mood"] = plan["music_mood"]

    info = media.probe(src)
    pipeline.see(cfg, src, info)
    c = cfg["clips"]
    start, end, peak = float(row["start"]), float(row["end"]), float(row["peak"])
    length = float(o.get("length") or (end - start))
    if plan.get("different_moment"):
        used = [(float(r["start"]), float(r["end"])) for r in db.clips() if r["source"] == row["source"]]
        for m in highlights.find_moments(src, info, length, count=8, min_gap=max(4.0, length * 0.5)):
            if all(min(m.end, b) - max(m.start, a) < 0.3 * length for a, b in used):
                start, end, peak = m.start, m.end, m.peak
                break
        else:
            plan["reply"] = (plan.get("reply") or "") + " There wasn't another strong moment in this clip, so I kept this one."
    if plan.get("shift"):
        sh = float(plan["shift"])
        sh = max(-start, min(sh, info.duration - end))
        start, end, peak = start + sh, end + sh, peak + sh
    if abs((end - start) - length) > 0.5:
        frac = (peak - start) / max(end - start, 1)
        start = max(0.0, min(peak - length * frac, info.duration - length))
        end = start + length
        peak = min(max(peak, start + 1), end - 1)

    style = o.get("style") or rmeta.get("style", "classic")
    pre = info.pre(start, end)
    stats = media.tone_stats(src, start, end - start, pre=pre)
    color = media.color_stats(src, start, end - start, pre=pre) if info.screen is not None else None
    look = looks.plan(cfg, row["game"], stats, ai_pick=o.get("look") or (rmeta.get("fx") or {}).get("look"),
                      impact=copy.get("impact", True) is not False, graphic=bool(copy.get("graphic_violence")),
                      filmed=info.screen is not None, color=color)
    looks.apply_overrides(look, o)
    moments = rmeta.get("moments") or []
    if style in editor.COMPILATIONS and len(moments) < editor.COMPILATIONS[style]:
        moments = [m.to_dict() for m in highlights.find_moments(src, info, 8, count=6, min_gap=6)]
    end_card = o["end_card"] if "end_card" in o else str(rmeta.get("end_card_text") or "")
    out = Path(row["file"]) if row["file"] else cfg.output / "clips" / clip_id / "reel.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    # sound: music (yours, or a beat made for this reel), game sound level, AI voice
    mcfg = cfg.get("music") or {}
    want_music = o.get("music")
    if want_music is None:
        want_music = bool(rmeta.get("music") or copy.get("ig_audio_plan"))
    track, music_note = None, ""
    ig_ready = bool(cfg.ig_user_id and cfg.ig_token)
    source = o.get("music_source") or ("instagram" if ig_ready and (cfg.get("instagram") or {}).get("trending_audio", True)
                                       else "agent")
    if not want_music:
        copy.pop("ig_audio_plan", None)
    elif source == "instagram":
        cur = copy.get("ig_audio_plan")
        if cur and not plan.get("new_song"):
            pass                                             # keep the Instagram sound already chosen
        elif not ig_ready:
            music_note = "no_instagram"
        else:
            picked = reach.plan_sound(cfg, db, copy, row["game"], [cur["audio_id"]] if cur else [])
            if picked:
                copy["ig_audio_plan"] = picked
            else:
                music_note = "ig_empty" if reach.LAST.get("why") == "empty" else "no_fit"
        if not copy.get("ig_audio_plan"):                  # Instagram can't supply one: the reel gets a beat
            track = "beat"
    else:
        copy.pop("ig_audio_plan", None)
        mood = copy.get("music_mood") or "hype"
        track = None if plan.get("new_song") else mus.pick_track(cfg, row["game"], mood, [])
        if track is None:
            track = "beat"
    game_level = {"off": 0.0, "low": 0.25, "normal": None}.get(o.get("game_audio") or "normal")
    voice = None
    if o.get("voiceover") is True or (o.get("voiceover") is None and rmeta.get("voice") and not want_music):
        lines = copy.get("voiceover") or []
        ready, _ = voiceover.engine_ready(cfg)
        if lines and ready:
            try:
                if voiceover.build(cfg, lines, end - start, out.parent / "voiceover.wav"):
                    voice = out.parent / "voiceover.wav"
            except Exception:
                voice = None
    spec = editor.RenderSpec(
        source=src, info=info, start=start, end=end, peak=peak,
        layout=o.get("layout") or rmeta.get("layout", "blur"), hook=copy["hook_text"], handle=cfg.get("handle", ""),
        words=[], teaser=o.get("teaser", rmeta.get("teaser", True)),
        punch=o.get("punch_zoom", bool(c.get("punch_zoom", True))),
        fps=int(c["fps"]), crf=16 if o.get("quality") == "high" else int(c["crf"]),
        preset="fast" if o.get("quality") == "high" else c.get("preset", "veryfast"), font=c.get("font", "Anton"),
        music=track if isinstance(track, Path) else None,
        music_volume={"low": 0.35, "high": 0.8}.get(o.get("music_volume"), float(mcfg.get("volume", 0.55))),
        game_volume=float(mcfg.get("game_volume", 0.6)) if game_level is None else game_level,
        game_volume_alone=1.0 if game_level is None else game_level,
        voice=voice,
        style=style, meme_line=str(copy.get("meme_line") or ""), moments=moments,
        freeze_line=str(copy.get("freeze_line") or ""), end_card=end_card, look=look,
        encoder=c.get("encoder", "auto"), cut_seconds=formats.cut_seconds(cfg, row["game"]),
        text_scale=float(o.get("text_scale") or c.get("text_scale", 1.0)),
    )
    if track == "beat":
        tl = editor.timeline(spec)
        spec.music = beats.make(copy.get("music_mood") or "hype", tl["total"] + 0.5, tl["payoff"],
                                out.parent / "beat.wav", seed=int(rmeta.get("version", 1)) + 7 + (100 if plan.get("new_song") else 0))
    elif isinstance(track, Path) and mcfg.get("sync_drop", True):
        spec.music_seek, spec.music_delay = mus.alignment(track, editor.payoff_time(spec))
    result = editor.render(spec, out)
    media.grab_frame(out, result["cover_ms"] / 1000, out.parent / "cover.jpg", width=1080)
    (out.parent / "instagram_caption.txt").write_text(captions.instagram_text(copy), encoding="utf-8")
    rmeta.update(result)
    rmeta.update(overrides=o, layout=spec.layout, teaser=spec.teaser, end_card_text=end_card, moments=moments,
                 version=int(rmeta.get("version", 1)) + 1, beat_seed=int(rmeta.get("version", 1)) + 7,
                 music=(f"beat:{copy.get('music_mood') or 'hype'}" if track == "beat" else str(track) if track else None),
                 voice=str(voice) if voice else None)
    result["has_music"] = bool(spec.music) or bool(copy.get("ig_audio_plan"))
    result["has_voice"], result["game_level"] = bool(voice), game_level
    result["ig_sound"], result["music_note"] = copy.get("ig_audio_plan"), music_note
    copy["approved"] = False                                  # a new version always needs a fresh OK
    copy["edit_style"] = result["style"]
    db.update(clip_id, copy_json=copy, render_json=rmeta, start=start, end=end, peak=peak, file=str(out),
              status="ready" if (cfg.get("review") or {}).get("require_approval", True) else "queued")
    return result


def handle(cfg, db: DB, clip_id: str, text: str, src: Path | None) -> str:
    """Read the owner's note, re-make the reel, reply in the chat. Returns the reply.
    The reply is built from what was actually done to the new version, never from what was promised."""
    row = db.get(clip_id)
    plan = read_note(cfg, row, text)
    changes = {k: v for k, v in plan.items() if k not in ("reply", "cannot", "remember", "note_for_future")}
    wait = "ready" if (cfg.get("review") or {}).get("require_approval", True) else "queued"
    if plan.get("remember"):
        remember(cfg, plan)
    if not changes:
        reply = plan.get("reply") if not cfg.ai_available and plan.get("reply") else \
            "I couldn't work out what to change from that. Could you say it another way, for example \"remove the voiceover\" or \"start earlier\"?"
    elif not (set(changes) - {"caption", "hashtags"}):                  # words only: no need to re-edit the video
        copy = copy_of(row)
        if "caption" in changes:
            copy["instagram_caption"] = changes["caption"]
        if "hashtags" in changes:
            copy["hashtags"] = changes["hashtags"]
        copy["approved"] = False
        db.update(clip_id, copy_json=copy, status=wait)
        reply = f"Done: {describe(changes)}."
    elif src is None or not Path(src).exists():
        reply = ("I can't re-edit this one: its original clip is no longer stored. "
                 "Add the gameplay again and I'll make it with your notes.")
    else:
        try:
            res = remake(cfg, db, clip_id, plan, Path(src))
            done, missed = [], []
            for k, v in changes.items():
                if k == "music" and v and not res.get("has_music"):
                    missed.append("music (no track could be made)")
                elif k == "voiceover" and v and not res.get("has_voice"):
                    missed.append("an AI voiceover (the voice engine isn't available)")
                else:
                    done.append(k)
            for k in ("music_source", "new_song"):
                if k in done and res.get("music_note"):
                    done.remove(k)
            said = describe({k: changes[k] for k in done if k != "music_source"})
            reply = f"New version ready: {said}." if said else "New version ready."
            if missed:
                reply += " I couldn't add " + " and ".join(missed) + "."
            ig = res.get("ig_sound")
            if ig and (changes.get("music") or changes.get("music_source") or changes.get("new_song")):
                reply += (f" Music: \"{ig.get('title', '')}\"" + (f" by {ig['artist']}" if ig.get("artist") else "")
                          + " from Instagram. Instagram adds it when the reel is posted, so the preview here plays "
                            "without it.")
            elif res.get("music_note") == "no_instagram":
                reply += (" I can't take music from Instagram until Instagram is connected (Settings, Connect "
                          "Instagram), so for now it has a beat I made for it, with the drop on the big moment.")
            elif res.get("music_note") == "ig_empty":
                reply += (" I asked Instagram for music, but its music search for apps returned no songs for your "
                          "account right now (Instagram only gives apps the sounds it has cleared for them, and none "
                          "came back). So it has a beat I made for it. I check again twice a day.")
            elif res.get("music_note") == "no_fit":
                reply += " Instagram had no sound that fits this clip right now, so it has a beat I made for it."
            elif changes.get("music") and res.get("has_music"):
                reply += " The music is a beat I made for this reel, with the drop on the big moment."
        except Exception as e:
            reply = f"The re-edit failed, so the reel is unchanged: {str(e)[-200:]}"
    if plan.get("cannot"):
        reply += f" Not possible yet: {plan['cannot']}"
    if plan.get("remember"):
        reply += " I'll do this on every reel from now on."
    add_chat(db, clip_id, "agent", reply.strip())
    return reply
