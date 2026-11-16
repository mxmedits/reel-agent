"""Account management jobs built on the vendored playbooks.

  replies   ig-reply  : triage new comments on recent reels, answer the ones worth answering,
                        hide abusive ones, ignore noise. Replies go through the humanizer.
  audit     ig-audit + ig-viral/swipe : pull each reel's stats, rank by outlier multiple and
                        sends per reach, find what separates top from bottom, write a report and
                        learnings.json (fed back into caption writing and clip picking).
  profile   ig-profile: score the bio against the 100-point rubric and write the rewrite.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import toolkit
from .captions import ask_claude, parse_json
from .db import DB, copy_of

REPLY_RULES = """
You run this gaming page's comment section on autopilot, replying publicly as the page.
Autopilot rules (these override anything above):
- Buckets: SUBSTANCE, QUESTION, SUPPORT, NOISE, HATER, ABUSIVE (there are no KEYWORD or LEAD asks on this page).
- Reply to SUBSTANCE and QUESTION. Reply to SUPPORT only with 2-8 words, and only some of them.
- NOISE and HATER: reply null. ABUSIVE (slurs, threats, harassment, scam links): reply null, hide true.
- Match their length. Gaming banter is fine. At most one emoji. No links, no DMs, no giveaways, no promises.
- Only state facts about the clip that the clip context below supports. If you don't know, say so or skip.
- If someone asks whether the page or its replies are automated, answer honestly.
- Never reply twice in one thread. Never argue.
Return ONLY a JSON list: [{"id": "<comment id>", "bucket": "...", "reply": "<text>" or null, "hide": false}]
"""


def _utc_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def reply_round(cfg, db: DB, ig, dry_run: bool = False, log=print) -> int:
    e = cfg.get("engagement", {})
    if not e.get("reply_to_comments", True) or not cfg.ai_available:
        return 0
    daily_cap = int(e.get("max_replies_per_day", 60))
    budget = min(int(e.get("max_replies_per_run", 15)), daily_cap - db.replies_since(_utc_days_ago(1)))
    if budget <= 0:
        return 0
    me = (ig.account().get("username") or "").lower()
    playbook = toolkit.playbook("ig-reply")
    sent = 0
    for clip in db.posted_since(_utc_days_ago(int(e.get("reply_window_days", 7)))):
        if not clip["ig_id"] or budget <= 0:
            continue
        todo = []
        for c in ig.comments(clip["ig_id"]):
            if (c.get("username") or "").lower() == me or db.was_replied(c["id"]):
                continue
            if any((r.get("username") or "").lower() == me for r in (c.get("replies") or {}).get("data", [])):
                continue
            todo.append({"id": c["id"], "user": c.get("username"), "text": c.get("text", ""),
                         "likes": c.get("like_count", 0)})
        if not todo:
            continue
        copy = copy_of(clip)
        context = (f"Clip context: game={clip['game']}; what happens: {copy.get('what_happens') or 'n/a'}; "
                   f"on-screen hook: {copy.get('hook_text')}; caption: {copy.get('instagram_caption')}")
        prompt = f"{context}\n\nNew comments:\n{json.dumps(todo[:30], ensure_ascii=False)}"
        try:
            plan = parse_json(ask_claude(cfg, playbook + "\n" + REPLY_RULES, [{"type": "text", "text": prompt}], 1500))
        except Exception as ex:
            log(f"  ! reply planning failed: {ex}")
            continue
        valid = {t["id"] for t in todo}
        for item in plan if isinstance(plan, list) else []:
            cid = str(item.get("id", ""))
            if cid not in valid:
                continue
            hide = bool(item.get("hide")) and e.get("hide_abusive", True)
            text = item.get("reply")
            if text and budget > 0:
                text, _ = toolkit.humanize(str(text))
                text = text[:300]
            else:
                text = None
            if dry_run:
                log(f"  [dry-run] {item.get('bucket')}: {cid} → {text!r}{' (hide)' if hide else ''}")
                continue
            try:
                if hide:
                    ig.hide(cid)
                if text:
                    ig.reply(cid, text)
                    sent += 1
                    budget -= 1
                db.mark_replied(cid, clip["id"], str(item.get("bucket", "")), text, hide)
            except Exception as ex:
                log(f"  ! could not act on comment {cid}: {ex}")
    if sent:
        log(f"💬 replied to {sent} comment(s)")
    return sent


# ── audit ───────────────────────────────────────────────────────────────────

def refresh_insights(cfg, db: DB, ig, days: int = 60, log=print) -> int:
    n = 0
    for clip in db.posted_since(_utc_days_ago(days)):
        if clip["ig_id"]:
            try:
                db.save_insights(clip["id"], ig.insights(clip["ig_id"]))
                n += 1
            except Exception as ex:
                log(f"  ! insights for {clip['id']} failed: {ex}")
    return n


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def run_audit(cfg, db: DB, log=print) -> dict:
    rows = [dict(r) for r in db.performance()]
    rows = [r for r in rows if r.get("views") or r.get("reach")]
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = cfg.output / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    if len(rows) < 3:
        msg = f"Only {len(rows)} reel(s) with stats so far. The audit needs at least 3; it will run again later."
        log(f"📊 {msg}")
        return {"n": len(rows), "note": msg}

    median_views = statistics.median([r.get("views") or r.get("reach") or 0 for r in rows]) or 1
    for r in rows:
        c = copy_of(r)
        v = r.get("views") or r.get("reach") or 0
        r["outlier"] = round(v / median_views, 2)
        r["sends_per_reach"] = round((r.get("shares") or 0) / r["reach"], 4) if r.get("reach") else None
        r["saves_per_reach"] = round((r.get("saved") or 0) / r["reach"], 4) if r.get("reach") else None
        r["hook"] = c.get("hook_text", "")
        r["formula"] = c.get("hook_formula") or toolkit.classify_hook(r["hook"])[1]
        r["hook_score"] = c.get("hook_score")
        rj = json.loads(r.get("render_json") or "{}")
        r["teaser"] = rj.get("teaser")
        r["style"] = rj.get("style", "classic")
        r["sound"] = ("instagram sound" if c.get("ig_audio") else "library music" if rj.get("music")
                      else "voiceover" if rj.get("voice") else "game audio only")
        r["trial"] = "trial reel" if c.get("trial") else "regular"
    ranked = sorted(rows, key=lambda r: (r["outlier"], r["sends_per_reach"] or 0), reverse=True)
    third = max(1, len(ranked) // 3)
    top, bottom = ranked[:third], ranked[-third:]

    def group(key):
        g = {}
        for r in rows:
            g.setdefault(str(r.get(key)), []).append(r["outlier"])
        return {k: {"n": len(v), "mean_outlier": _mean(v)} for k, v in sorted(g.items())}

    by_game, by_formula = group("game"), group("formula")
    by_style, by_sound, by_trial = group("style"), group("sound"), group("trial")
    confidence = "low (fewer than 10 reels)" if len(rows) < 10 else ("medium" if len(rows) < 30 else "good")
    findings = []
    ts, bs = _mean([r["hook_score"] for r in top]), _mean([r["hook_score"] for r in bottom])
    if ts is not None and bs is not None:
        findings.append(f"Hook score: top third averages {ts}, bottom third {bs}.")
    tw, bw = _mean([r.get("avg_watch_ms") for r in top]), _mean([r.get("avg_watch_ms") for r in bottom])
    if tw and bw:
        findings.append(f"Average watch time: top {tw / 1000:.1f}s vs bottom {bw / 1000:.1f}s.")
    best_game = max(by_game.items(), key=lambda kv: kv[1]["mean_outlier"] or 0)
    findings.append(f"Best game so far: {best_game[0]} ({best_game[1]['mean_outlier']}x median, n={best_game[1]['n']}).")
    tf = {}
    for r in top:
        tf[r["formula"]] = tf.get(r["formula"], 0) + 1
    if tf:
        findings.append("Hook formulas in the top third: " + ", ".join(f"{k} ({v})" for k, v in
                                                                         sorted(tf.items(), key=lambda kv: -kv[1])))
    tt, nt = [r["outlier"] for r in rows if r["teaser"]], [r["outlier"] for r in rows if not r["teaser"]]
    if tt and nt:
        findings.append(f"Teaser hook on: {_mean(tt)}x vs off: {_mean(nt)}x (n={len(tt)}/{len(nt)}).")

    def line(r):
        return (f"  {r['outlier']:>5.1f}x  {r['id']:<12} {str(r.get('views') or r.get('reach')):>8} views  "
                f"sends/reach {r['sends_per_reach'] or 0:.3f}  [{r['formula']}] \"{r['hook']}\"")

    report = [f"AUDIT  ·  {len(rows)} reels  ·  median views {median_views:,.0f}  ·  confidence {confidence}", "",
              "TOP BY OUTLIER MULTIPLE", *[line(r) for r in ranked[:5]], "",
              *(["BOTTOM", *[line(r) for r in ranked[-5:]], ""] if len(ranked) > 5 else []),
              "WHAT THE DATA SAYS", *[f"{i}. {f}" for i, f in enumerate(findings, 1)], "",
              "BY GAME", *[f"  {k}: {v['mean_outlier']}x (n={v['n']})" for k, v in by_game.items()], "",
              "BY HOOK FORMULA", *[f"  {k}: {v['mean_outlier']}x (n={v['n']})" for k, v in by_formula.items()], "",
              "BY EDIT STYLE", *[f"  {k}: {v['mean_outlier']}x (n={v['n']})" for k, v in by_style.items()], "",
              "BY SOUND", *[f"  {k}: {v['mean_outlier']}x (n={v['n']})" for k, v in by_sound.items()], "",
              "TRIAL VS REGULAR", *[f"  {k}: {v['mean_outlier']}x (n={v['n']})" for k, v in by_trial.items()]]
    queued = db.clips("queued")
    report += ["", f"NEXT UP (ig-plan): {len(queued)} reel(s) queued, posting at {', '.join(cfg['schedule']['post_times'])}"]
    report += [f"  {q['id']}: \"{copy_of(q).get('hook_text')}\" (quality {q['quality']})" for q in queued[:7]]
    text = "\n".join(report)
    (out_dir / f"audit-{today}.md").write_text("```\n" + text + "\n```\n", encoding="utf-8")

    notes = ["- Hooks that beat this page's median: " + "; ".join(f"\"{r['hook']}\" ({r['outlier']}x)" for r in top[:5]),
             "- Hooks that flopped: " + "; ".join(f"\"{r['hook']}\" ({r['outlier']}x)" for r in bottom[:3])]
    if tf:
        notes.append("- Formulas winning here: " + ", ".join(tf))
    ranked_styles = sorted(by_style.items(), key=lambda kv: -(kv[1]["mean_outlier"] or 0))
    if len(ranked_styles) > 1:
        notes.append("- Edit styles on this page (views vs median): " +
                     ", ".join(f"{k} {v['mean_outlier']}x (n={v['n']})" for k, v in ranked_styles))
    learn = {"updated": today, "n": len(rows), "confidence": confidence,
             "game_weights": {k: v["mean_outlier"] for k, v in by_game.items()},
             "prompt_notes": "\n".join(notes) if len(rows) >= 5 else ""}
    (cfg.state_dir / "learnings.json").write_text(json.dumps(learn, indent=2), encoding="utf-8")
    log(f"📊 audit written: reports/audit-{today}.md")
    return {"n": len(rows), "report": text, "learnings": learn}


# ── profile ─────────────────────────────────────────────────────────────────

def profile_check(cfg, ig, log=print) -> Path | None:
    if not cfg.ai_available:
        log("profile check needs ANTHROPIC_API_KEY")
        return None
    prof = ig.profile()
    recent = [m.get("caption", "")[:120] for m in ig.recent_media(9)]
    prompt = (f"Profile (from the API):\n{json.dumps(prof, ensure_ascii=False)}\n\n"
              f"Recent captions:\n{json.dumps(recent, ensure_ascii=False)}\n\n"
              f"Rubric:\n{json.dumps(toolkit.profile_rubric(), ensure_ascii=False)}\n\n"
              "This is a gaming clips page (GTA 6, Call of Duty, EA FC). Score it out of 100 against the rubric, "
              "item by item, then give the rewritten name field and bio, fix-first. The bio cannot be changed "
              "through the API, so end with a copy-paste block the owner can paste once. Markdown.")
    text = ask_claude(cfg, toolkit.playbook("ig-profile"), [{"type": "text", "text": prompt}], 2500)
    out = cfg.output / "reports" / f"profile-{datetime.now():%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    log(f"🪪 profile report written: reports/{out.name}")
    return out
