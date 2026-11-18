"""Reel Agent command line.  python -m reelagent <command>

  cloud              GitHub mode: one visit (what the workflow runs every 15 minutes)
  phone [FILES]      iPhone: edit shared clips into reels, post one if it's a good time  [--game]
  phone-tick         iPhone automations: post a due reel, answer comments, refresh trends
  phone-setup        iPhone: install packages, keys, Instagram, speed test (one time)
  update             download the latest version of the agent from GitHub (keeps your data)
  scout              read Instagram's top posts now: trending hashtags, captions, posting times
  serve              phone dashboard + autopilot (what the cloud server runs)
  watch              autopilot only (no dashboard)
  setup-instagram    connect your Instagram account (one time)
  doctor             check everything is installed and connected
  run                process new recordings in the inbox once, then exit
  process FILE       make reels from one recording now  [--game gta6|cod|fc27]
  queue              list reels and their status
  post-now [ID]      post the next (or a specific) reel immediately
  approve ID|all     (only if review.require_approval is true)
  reject ID          drop a reel from the queue
  rehook ID "TEXT"   re-render a reel with a different on-screen hook
  replies            answer new comments now
  audit              pull stats and write the performance report now
  profile            score your profile against the 100-point rubric now
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import autopilot, engagement, instagram, media, phone, pipeline, scout, transcribe, voiceover
from . import music as mus
from . import toolkit
from .config import Config
from .db import DB, copy_of


def _db(cfg) -> DB:
    return DB(cfg.state_dir / "agent.db")


def cmd_doctor(cfg, a):
    ok = True

    def line(good, label, hint=""):
        nonlocal ok
        ok &= bool(good) or label.startswith("(optional)")
        print(f"  {'✅' if good else ('➖' if label.startswith('(optional)') else '❌')} {label}"
              + (f"  → {hint}" if hint and not good else ""))

    print(f"Config: {cfg.source}")
    line(media.tool_ok("ffmpeg"), "FFmpeg installed",
         "iPhone: pkg install ffmpeg | Windows: winget install Gyan.FFmpeg | Mac: brew install ffmpeg")
    if media.tool_ok("ffmpeg"):
        line(media.has_filter("ass"), "FFmpeg can burn captions (libass)", "install a 'full' FFmpeg build")
        line(media.has_filter("sidechaincompress"), "FFmpeg can duck audio", "install a 'full' FFmpeg build")
        line(True, f"video encoder: {media.video_encoder(cfg['clips'].get('encoder', 'auto'))}")
        from . import looks
        missing = [f for f in ("colorbalance", "curves", "unsharp", "gblur", "blend", "vignette", "noise",
                               "chromashift", "drawbox", "hqdn3d", "colorlevels", "vibrance") if not media.has_filter(f)]
        line(not missing, "pro looks (grade, sharpen, glow, grain, impact FX)",
             f"this ffmpeg lacks {', '.join(missing)}; those touches are skipped")
    line(toolkit.available(), "instagram-agent-skill pack (vendor/)", "re-extract the zip")
    from . import ai
    prov = ai.provider(cfg)
    line(prov != "none", f"AI brain: {prov}", "add GEMINI_API_KEY (free) or ANTHROPIC_API_KEY to .env")
    line(bool(os.getenv("YOUTUBE_API_KEY")), "(optional) YouTube API key for free trend data", "add YOUTUBE_API_KEY")
    line(cfg.ig_user_id and cfg.ig_token, "Instagram connected", "python -m reelagent setup-instagram")
    if cfg.ig_user_id and cfg.ig_token:
        try:
            ig = instagram.from_config(cfg)
            acct = ig.account()
            line(True, f"Instagram API works: @{acct.get('username')} ({acct.get('followers_count', '?')} followers)")
            lim = ig.publishing_limit().get("data", [{}])[0]
            print(f"     publishing quota used: {lim.get('quota_usage', '?')} / "
                  f"{(lim.get('config') or {}).get('quota_total', '?')} per 24h")
        except Exception as e:
            line(False, "Instagram API works", str(e))
    vo_ok, why = voiceover.engine_ready(cfg)
    line(vo_ok, f"(optional) voiceover engine: {why}", "pip install piper-tts")
    line(transcribe.available(), "(optional) faster-whisper for your commentary captions", "pip install faster-whisper")
    mus.ensure_library(cfg)
    n = sum(1 for p in mus.library(cfg).rglob("*") if p.suffix.lower() in mus.AUDIO_EXT)
    line(n > 0, f"(optional) music library: {n} track(s) in {mus.library(cfg)}",
         "add royalty-free tracks to the mood folders")
    print(f"  📥 inbox: {cfg.inbox}")
    print("\nAll set." if ok else "\nFix the ❌ items above.")


def cmd_queue(cfg, a):
    rows = _db(cfg).clips()
    if not rows:
        print("Queue is empty. Drop gameplay recordings into", cfg.inbox)
        return
    for r in rows:
        c = copy_of(r)
        extra = r["ig_url"] or r["error"] or ""
        print(f"{r['id']:<11} {r['status']:<8} q{r['quality']:<2} hook {c.get('hook_score', '-'):<5} "
              f"\"{c.get('hook_text', '')}\"  {extra}")


def cmd_post_now(cfg, a):
    db = _db(cfg)
    row = db.get(a.id) if a.id else autopilot.pick_next(cfg, db)
    if not row:
        sys.exit("Nothing queued to post.")
    autopilot.post(cfg, db, row, dry_run=a.dry_run)


def cmd_approve(cfg, a):
    db = _db(cfg)
    rows = db.clips("ready") if a.id == "all" else [db.get(a.id)]
    for r in rows:
        if r:
            db.update(r["id"], status="queued", copy_json={**copy_of(r), "approved": True})
            print("approved", r["id"])


def cmd_reject(cfg, a):
    db = _db(cfg)
    r = db.get(a.id)
    if not r:
        sys.exit(f"No clip {a.id}")
    db.update(a.id, status="rejected")
    print("rejected", a.id)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m reelagent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=120)
    w.add_argument("--dry-run", action="store_true", help="do everything except post")
    ph = sub.add_parser("phone")
    ph.add_argument("files", nargs="*")
    ph.add_argument("--game")
    ph.add_argument("--dry-run", action="store_true")
    pt = sub.add_parser("phone-tick")
    pt.add_argument("--dry-run", action="store_true")
    sub.add_parser("phone-setup")
    up = sub.add_parser("update")
    up.add_argument("--repo")
    sub.add_parser("scout")
    sub.add_parser("cloud")
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sub.add_parser("setup-instagram")
    sub.add_parser("doctor")
    sub.add_parser("run")
    p = sub.add_parser("process")
    p.add_argument("file")
    p.add_argument("--game")
    sub.add_parser("queue")
    pn = sub.add_parser("post-now")
    pn.add_argument("id", nargs="?")
    pn.add_argument("--dry-run", action="store_true")
    ap_ = sub.add_parser("approve")
    ap_.add_argument("id")
    rj = sub.add_parser("reject")
    rj.add_argument("id")
    rh = sub.add_parser("rehook")
    rh.add_argument("id")
    rh.add_argument("hook")
    rp = sub.add_parser("replies")
    rp.add_argument("--dry-run", action="store_true")
    sub.add_parser("audit")
    sub.add_parser("profile")
    a = ap.parse_args(argv)
    if a.cmd == "cloud":       # GitHub mode loads its records first, so it builds its own settings
        from . import cloud
        sys.exit(cloud.run(a.config))
    cfg = Config(a.config)

    if a.cmd == "phone":
        phone.run(cfg, a.files, a.game, a.dry_run)
    elif a.cmd == "phone-tick":
        phone.tick(cfg, a.dry_run)
    elif a.cmd == "phone-setup":
        phone.setup(cfg)
    elif a.cmd == "update":
        phone.update(cfg, a.repo)
    elif a.cmd == "scout":
        d = scout.refresh(cfg, instagram.from_config(cfg))
        print(scout.evidence_text(cfg) or "No Instagram data yet: " + "; ".join(d.get("notes", [])))
    elif a.cmd == "watch":
        try:
            autopilot.watch(a.config, a.interval, a.dry_run)
        except KeyboardInterrupt:
            print("\nstopped.")
    elif a.cmd == "serve":
        from . import web
        web.serve(a.config, a.host, a.port)
    elif a.cmd == "setup-instagram":
        instagram.setup_interactive(cfg)
    elif a.cmd == "doctor":
        cmd_doctor(cfg, a)
    elif a.cmd == "run":
        db = _db(cfg)
        recs = pipeline.new_recordings(cfg, db, settle_seconds=5)
        if not recs:
            print("No new recordings in", cfg.inbox)
        for r in recs:
            pipeline.process_file(cfg, db, r)
    elif a.cmd == "process":
        pipeline.process_file(cfg, _db(cfg), Path(a.file), a.game)
    elif a.cmd == "queue":
        cmd_queue(cfg, a)
    elif a.cmd == "post-now":
        cmd_post_now(cfg, a)
    elif a.cmd == "approve":
        cmd_approve(cfg, a)
    elif a.cmd == "reject":
        cmd_reject(cfg, a)
    elif a.cmd == "rehook":
        pipeline.rerender(cfg, _db(cfg), a.id, a.hook)
    elif a.cmd == "replies":
        engagement.reply_round(cfg, _db(cfg), instagram.from_config(cfg), dry_run=a.dry_run)
    elif a.cmd == "audit":
        db = _db(cfg)
        ig = instagram.from_config(cfg)
        engagement.refresh_insights(cfg, db, ig)
        r = engagement.run_audit(cfg, db)
        print(r.get("report") or r.get("note"))
    elif a.cmd == "profile":
        engagement.profile_check(cfg, instagram.from_config(cfg))


if __name__ == "__main__":
    main()
