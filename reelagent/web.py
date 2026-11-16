"""Phone dashboard: control the whole agent from a browser on your phone.

python -m reelagent serve   → dashboard on http://127.0.0.1:8080 + autopilot in the background.
On the cloud server, Caddy puts it on https://<your-ip>.sslip.io with a real certificate.
Login with DASHBOARD_PASSWORD from .env.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import shutil
import threading
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from . import autopilot, captions, engagement, instagram, pipeline, reach, trends, voiceover
from . import music as mus
from .config import HOME, Config, load_env, update_env
from .db import DB, copy_of

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
CONFIG_PATH: str | None = None
COOKIE = "ra_session"
_pending_accounts: dict[str, list] = {}
E = html.escape


# ── helpers ─────────────────────────────────────────────────────────────────

def _cfg() -> Config:
    load_env()
    return Config(CONFIG_PATH)


def _db(cfg: Config) -> DB:
    return DB(cfg.state_dir / "agent.db")


def _secret() -> bytes:
    f = HOME / "output" / "state" / ".web_secret"
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        f.write_bytes(secrets.token_bytes(32))
    return f.read_bytes() + (os.getenv("DASHBOARD_PASSWORD") or "").encode()


def _sign(exp: int) -> str:
    return f"{exp}.{hmac.new(_secret(), str(exp).encode(), hashlib.sha256).hexdigest()}"


def _authed(req: Request) -> bool:
    tok = req.cookies.get(COOKIE, "")
    try:
        exp = int(tok.split(".", 1)[0])
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(tok, _sign(exp))


def _go(path: str, msg: str = "") -> RedirectResponse:
    return RedirectResponse(path + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


def _bg(fn, *args):
    """Run a heavy job in the background, one at a time with the autopilot."""
    def run():
        with autopilot.JOB_LOCK:
            autopilot.STATE["busy"] = getattr(fn, "__name__", "job")
            try:
                fn(*args)
            except Exception as e:
                pipeline.log(f"! {getattr(fn, '__name__', 'job')} failed: {e}", _cfg())
            finally:
                autopilot.STATE["busy"] = ""
    threading.Thread(target=run, daemon=True).start()


def _next_slot(cfg) -> str:
    tz = autopilot._tz(cfg)
    now = datetime.now(tz)
    for add in (0, 1):
        day = (now + timedelta(days=add)).date()
        for hhmm in sorted(reach.post_times(cfg)):
            h, m = map(int, str(hhmm).split(":"))
            t = datetime.combine(day, dtime(h, m), tzinfo=tz)
            if t > now:
                return ("today " if add == 0 else "tomorrow ") + t.strftime("%H:%M")
    return "—"


CSS = """
:root{--bg:#0a0c11;--card:#141823;--line:#242b3b;--text:#eef1f7;--muted:#8d97ab;--accent:#ff3d71;--ok:#22d07a;--warn:#ffb020;--bad:#ff5a5a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding-bottom:84px}
main{max-width:640px;margin:0 auto;padding:14px}h1{font-size:22px;margin:6px 0 14px}h2{font-size:17px;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.stat b{display:block;font-size:20px}.stat span{color:var(--muted);font-size:13px}
.muted{color:var(--muted);font-size:14px}a{color:#7fb2ff}
input,select,textarea{width:100%;background:#0e121b;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:11px;font-size:16px;margin:4px 0 10px}
label{font-size:13px;color:var(--muted)}textarea{min-height:110px}
.btn{display:inline-block;border:0;border-radius:12px;padding:12px 16px;font-size:15px;font-weight:600;background:var(--accent);color:#fff;text-decoration:none;text-align:center;cursor:pointer}
.btn.alt{background:#242b3b}.btn.ok{background:var(--ok);color:#06210f}.btn.full{width:100%}.row{display:flex;gap:8px;flex-wrap:wrap}.row .btn{flex:1}
.badge{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;background:#242b3b;color:var(--muted)}
.b-queued{background:#2a2350;color:#b9a7ff}.b-posted{background:#0f3323;color:var(--ok)}.b-failed,.b-rejected{background:#3a1717;color:var(--bad)}.b-ready{background:#3a2c0b;color:var(--warn)}
.clip{display:flex;gap:12px}.clip img{width:84px;height:150px;object-fit:cover;border-radius:10px;background:#000;flex:none}
.hook{font-weight:700}.flash{background:#14324a;border:1px solid #1e4d70;border-radius:12px;padding:10px 12px;margin-bottom:12px}
pre{white-space:pre-wrap;word-break:break-word;font-size:12.5px;background:#0e121b;border-radius:10px;padding:10px;max-height:60vh;overflow:auto}
nav{position:fixed;bottom:0;left:0;right:0;background:#0f131c;border-top:1px solid var(--line);display:flex;justify-content:space-around;padding:8px 4px calc(8px + env(safe-area-inset-bottom))}
nav a{color:var(--muted);text-decoration:none;font-size:11px;text-align:center;flex:1}nav a b{display:block;font-size:21px}nav a.on{color:var(--text)}
progress{width:100%;height:10px}video{width:100%;border-radius:12px;background:#000;max-height:70vh}
"""

NAV = [("/", "🏠", "Home"), ("/queue", "🎬", "Reels"), ("/music", "🎵", "Music"), ("/reports", "📊", "Stats"),
       ("/settings", "⚙️", "Settings")]


def page(title: str, body: str, active: str = "/", msg: str = "") -> HTMLResponse:
    nav = "".join(f'<a href="{h}" class="{"on" if h == active else ""}"><b>{i}</b>{t}</a>' for h, i, t in NAV)
    flash = f'<div class="flash">{E(msg)}</div>' if msg else ""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="theme-color" content="#0a0c11">
<title>{E(title)} · Reel Agent</title><style>{CSS}</style></head><body><main>{flash}{body}</main><nav>{nav}</nav>
<script>
function act(url, confirmText){{ if(confirmText && !confirm(confirmText)) return;
  fetch(url,{{method:'POST'}}).then(r=>r.json()).then(j=>{{ location.href = location.pathname + '?msg=' + encodeURIComponent(j.msg||'Done'); }}); }}
function upload(input, url, bar, out){{ const files=[...input.files]; if(!files.length) return; let i=0;
  const next=()=>{{ if(i>=files.length){{ out.textContent='✅ Uploaded '+files.length+' file(s)'; setTimeout(()=>location.reload(),900); return; }}
    const f=files[i++]; const x=new XMLHttpRequest(); x.open('PUT', url()+'&name='+encodeURIComponent(f.name));
    x.upload.onprogress=e=>{{ if(e.lengthComputable){{ bar.value=e.loaded/e.total*100; out.textContent='Uploading '+f.name+' — '+Math.round(e.loaded/e.total*100)+'%'; }} }};
    x.onload=()=>{{ if(x.status==200) next(); else out.textContent='❌ '+x.responseText; }}; x.onerror=()=>out.textContent='❌ upload failed — check connection';
    x.send(f); }}; next(); }}
</script></body></html>""")


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path in ("/login", "/healthz") or _authed(request):
        return await call_next(request)
    if request.url.path.startswith("/api/") or request.method != "GET":
        return JSONResponse({"msg": "Please log in again"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ── login ───────────────────────────────────────────────────────────────────

@app.get("/login")
def login_page(msg: str = ""):
    body = """<h1>🎮 Reel Agent</h1><div class="card"><form method="post" action="/login">
<label>Dashboard password</label><input type="password" name="password" autofocus autocomplete="current-password">
<button class="btn full">Log in</button></form></div>"""
    return page("Log in", body, "", msg)


@app.post("/login")
def login(password: str = Form("")):
    real = os.getenv("DASHBOARD_PASSWORD", "")
    if not real or not hmac.compare_digest(password.encode(), real.encode()):
        time.sleep(1.5)
        return _go("/login", "Wrong password")
    exp = int(time.time()) + 60 * 60 * 24 * 60
    r = _go("/")
    r.set_cookie(COOKIE, _sign(exp), max_age=60 * 60 * 24 * 60, httponly=True, samesite="strict",
                 secure=os.getenv("COOKIE_SECURE", "1") == "1")
    return r


@app.get("/logout")
def logout():
    r = _go("/login")
    r.delete_cookie(COOKIE)
    return r


# ── home ────────────────────────────────────────────────────────────────────

@app.get("/")
def home(msg: str = ""):
    cfg = _cfg()
    db = _db(cfg)
    connected = bool(cfg.ig_user_id and cfg.ig_token)
    queued = len(db.clips("queued"))
    ready = len(db.clips("ready"))
    today = len(db.posted_since(autopilot._local_midnight_utc(cfg)))
    last = db.last_posted()
    free_gb = shutil.disk_usage(HOME).free / 1e9
    is_paused = autopilot.paused(cfg)
    games = "".join(f'<option value="{E(k)}">{E(v.get("name", k))}</option>'
                    for k, v in cfg["games"].items() if k != "default")
    ig = (f'@{E(os.getenv("IG_USERNAME", "") or "connected")}' if connected
          else '<a href="/connect">Connect Instagram →</a>')
    setup = []
    if not cfg.ai_available:
        setup.append('<a href="/settings#keys">Add a free Gemini key or a Claude key</a> (writes hooks, captions, replies)')
    if not connected:
        setup.append('<a href="/connect">Connect Instagram</a> (needed to post)')
    if cfg.get("handle", "@yourpage") == "@yourpage":
        setup.append('<a href="/settings">Set your @handle</a> (watermark on every reel)')
    setup_html = ("<div class='card'><h2>Finish setup</h2><ol class='muted'>" +
                  "".join(f"<li>{s}</li>" for s in setup) + "</ol></div>") if setup else ""
    busy = autopilot.STATE.get("busy")
    log_tail = ""
    lf = cfg.state_dir / "agent.log"
    if lf.exists():
        log_tail = "\n".join(lf.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
    body = f"""<h1>🎮 Reel Agent</h1>{setup_html}
<div class="card grid">
 <div class="stat"><b>{ig}</b><span>Instagram</span></div>
 <div class="stat"><b>{"⏸ Paused" if is_paused else ("⚙️ Working" if busy else "🟢 On")}</b><span>Autopilot</span></div>
 <div class="stat"><b>{queued}</b><span>reels queued{f" · {ready} to approve" if ready else ""}</span></div>
 <div class="stat"><b>{today}/{cfg["schedule"]["max_per_day"]}</b><span>posted today</span></div>
 <div class="stat"><b>{_next_slot(cfg)}</b><span>next post slot</span></div>
 <div class="stat"><b>{free_gb:.0f} GB</b><span>free space</span></div>
</div>
{f'<div class="card"><span class="muted">Last post:</span> <a href="{E(last["ig_url"] or "#")}">{E(last["id"])}</a></div>' if last else ""}
<div class="card"><h2>📤 Add gameplay</h2>
 <label>Game</label><select id="game">{games}</select>
 <input type="file" id="vid" accept="video/*" multiple>
 <progress id="bar" value="0" max="100"></progress><div id="out" class="muted">Long recordings are fine. Use Wi-Fi for big files.</div>
 <button class="btn full" style="margin-top:10px" onclick="upload(document.getElementById('vid'), ()=>'/api/upload?game='+document.getElementById('game').value, document.getElementById('bar'), document.getElementById('out'))">Upload → make reels</button>
</div>
<div class="row" style="margin-bottom:12px">
 <a class="btn alt" onclick="act('/api/pause')">{"▶️ Resume" if is_paused else "⏸ Pause posting"}</a>
 <a class="btn alt" onclick="act('/api/run-now')">⚡ Run now</a>
</div>
<div class="card"><h2>Activity</h2><pre>{E(log_tail) or "Nothing yet."}</pre><a href="/log">Full log →</a></div>"""
    return page("Home", body, "/", msg)


@app.put("/api/upload")
async def api_upload(request: Request, game: str, name: str):
    cfg = _cfg()
    if game not in cfg["games"]:
        return JSONResponse({"msg": "unknown game"}, status_code=400)
    safe = "".join(c for c in Path(name).name if c.isalnum() or c in "._- ") or "clip.mp4"
    if Path(safe).suffix.lower() not in pipeline.VIDEO_EXT:
        safe += ".mp4"
    folder = cfg.inbox / game
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}"
    tmp = folder / f".part-{final.name}"
    with open(tmp, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    tmp.rename(final)
    old = time.time() - 600                     # mark as finished so the next cycle picks it up
    os.utime(final, (old, old))
    autopilot.WAKE.set()
    return {"msg": f"Uploaded {final.name}. Making reels now; they'll appear under Reels in a few minutes."}


@app.post("/api/pause")
def api_pause():
    cfg = _cfg()
    data = cfg.user_config()
    ap = data.setdefault("autopilot", {})
    ap["paused"] = not autopilot.paused(cfg)
    Config.save_user_config(data)
    return {"msg": "Posting paused. Reels are still made." if ap["paused"] else "Autopilot resumed."}


@app.post("/api/run-now")
def api_run_now():
    autopilot.WAKE.set()
    return {"msg": "Running a cycle now (new uploads, due posts, comments)."}


# ── reels ───────────────────────────────────────────────────────────────────

@app.get("/queue")
def queue(msg: str = "", show: str = "active"):
    cfg = _cfg()
    rows = list(reversed(_db(cfg).clips()))
    if show == "active":
        rows = [r for r in rows if r["status"] in ("queued", "ready", "failed")]
    elif show == "posted":
        rows = [r for r in rows if r["status"] == "posted"]
    tabs = " · ".join(f'<a href="/queue?show={k}" style="{"font-weight:700" if k == show else ""}">{t}</a>'
                      for k, t in [("active", "Up next"), ("posted", "Posted"), ("all", "All")])
    cards = []
    for r in rows[:60]:
        c = copy_of(r)
        st = r["status"]
        buttons = []
        if st in ("queued", "ready", "failed"):
            buttons.append(f'<a class="btn ok" onclick="act(\'/api/clip/{r["id"]}/post\',\'Post this reel now?\')">Post now</a>')
            buttons.append(f'<a class="btn alt" onclick="act(\'/api/clip/{r["id"]}/reject\',\'Skip this reel?\')">Skip</a>')
        if st == "ready":
            buttons.insert(0, f'<a class="btn" onclick="act(\'/api/clip/{r["id"]}/approve\')">Approve</a>')
        link = f' · <a href="{E(r["ig_url"])}">open on Instagram</a>' if r["ig_url"] else ""
        cards.append(f"""<div class="card"><div class="clip">
<a href="/clip/{r['id']}"><img loading="lazy" src="/media/{r['id']}/cover.jpg"></a>
<div style="flex:1;min-width:0"><div><span class="badge b-{st}">{st}</span> <span class="muted">{E(r['id'])} · q{r['quality']}</span></div>
<div class="hook">“{E(c.get('hook_text', ''))}”</div>
<div class="muted" style="font-size:13px;max-height:62px;overflow:hidden">{E(c.get('instagram_caption', ''))}</div>
<div class="muted" style="font-size:12px">{E(r['error'] or '')}{link}</div></div></div>
<div class="row" style="margin-top:10px"><a class="btn alt" href="/clip/{r['id']}">▶ Watch / edit</a>{''.join(buttons)}</div></div>""")
    body = f"<h1>🎬 Reels</h1><p>{tabs}</p>" + ("".join(cards) or "<div class='card muted'>No reels here yet. Upload gameplay on Home.</div>")
    return page("Reels", body, "/queue", msg)


@app.get("/clip/{cid}")
def clip_page(cid: str, msg: str = ""):
    cfg = _cfg()
    r = _db(cfg).get(cid)
    if not r:
        return _go("/queue", "Not found")
    c = copy_of(r)
    tags = " ".join(c.get("hashtags", []))
    hooks = "".join(f"<li>{E(h['hook'])} — {h['score']}</li>" for h in c.get("hook_table", []))
    body = f"""<h1>{E(cid)} <span class="badge b-{r['status']}">{r['status']}</span></h1>
<video controls playsinline preload="metadata" poster="/media/{cid}/cover.jpg" src="/media/{cid}/reel.mp4"></video>
<div class="card" style="margin-top:12px"><h2>Caption</h2><form method="post" action="/api/clip/{cid}/caption">
<textarea name="caption">{E(c.get('instagram_caption', ''))}</textarea>
<label>Hashtags (max 5)</label><input name="hashtags" value="{E(tags)}">
<button class="btn full">Save caption</button></form>
<p class="muted">Caption check: {E(str((c.get('caption_lint') or {}).get('verdict', '-')))} · human score {E(str((c.get('human_score') or ['-'])[0]))}</p></div>
<div class="card"><h2>On-screen hook</h2><form method="post" action="/api/clip/{cid}/rehook">
<input name="hook" value="{E(c.get('hook_text', ''))}"><button class="btn full alt">Re-render with this hook (~1 min)</button></form>
<p class="muted">Options Claude wrote (hook score):</p><ul class="muted">{hooks}</ul></div>
<div class="card muted">What happens: {E(c.get('what_happens') or '—')}<br>
Edit style: {E(str(json_style(r)))} · Music mood: {E(str(c.get('music_mood') or '—'))} · Voiceover: {"yes" if c.get('voiceover') else "no"}<br>
{("Instagram sound: " + E(c['ig_audio'].get('title', '')) + " - " + E(c['ig_audio'].get('artist', ''))) if c.get('ig_audio') else ""}
{" · trial reel" if c.get('trial') else ""}</div>
<div class="row"><a class="btn ok" onclick="act('/api/clip/{cid}/post','Post this reel now?')">Post now</a>
<a class="btn alt" onclick="act('/api/clip/{cid}/reject','Skip this reel?')">Skip</a></div>"""
    return page(cid, body, "/queue", msg)


def json_style(r) -> str:
    import json as _json
    return (_json.loads(r["render_json"] or "{}")).get("style", "classic")


def _clip_file(cfg, cid: str, name: str) -> Path | None:
    if name not in ("reel.mp4", "cover.jpg") or "/" in cid or ".." in cid:
        return None
    p = cfg.output / "clips" / cid / name
    return p if p.exists() else None


@app.get("/media/{cid}/{name}")
def media_file(cid: str, name: str, request: Request):
    p = _clip_file(_cfg(), cid, name)
    if not p:
        return Response(status_code=404)
    size = p.stat().st_size
    ctype = "video/mp4" if name.endswith(".mp4") else "image/jpeg"
    rng = request.headers.get("range")
    start, end = 0, size - 1
    if rng and rng.startswith("bytes="):            # Safari needs byte ranges to play video
        a, _, b = rng[6:].partition("-")
        start = int(a) if a else max(0, size - int(b))
        end = int(b) if a and b else size - 1
        end = min(end, size - 1)

    def chunks():
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                data = f.read(min(1 << 20, left))
                if not data:
                    break
                left -= len(data)
                yield data

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "private, max-age=300"}
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(chunks(), status_code=206, media_type=ctype, headers=headers)
    return StreamingResponse(chunks(), media_type=ctype, headers=headers)


@app.post("/api/clip/{cid}/{action}")
def clip_action(cid: str, action: str, caption: str = Form(None), hashtags: str = Form(None), hook: str = Form(None)):
    cfg = _cfg()
    db = _db(cfg)
    r = db.get(cid)
    if not r:
        return JSONResponse({"msg": "not found"}, status_code=404)
    if action == "post":
        if not (cfg.ig_user_id and cfg.ig_token):
            return {"msg": "Connect Instagram first (Settings → Instagram)."}
        _bg(lambda: autopilot.post(cfg, _db(cfg), _db(cfg).get(cid)))
        return {"msg": f"Posting {cid}… it appears on Instagram in a minute or two."}
    if action == "reject":
        db.update(cid, status="rejected")
        return {"msg": f"Skipped {cid}."}
    if action == "approve":
        db.update(cid, status="queued")
        return {"msg": f"Approved {cid}; it posts at the next slot."}
    if action == "caption":
        c = copy_of(r)
        c["instagram_caption"] = (caption or "").strip()
        c["hashtags"] = captions._clean_hashtags((hashtags or "").split(), [])
        db.update(cid, copy_json=c)
        folder = Path(r["file"]).parent
        (folder / "instagram_caption.txt").write_text(captions.instagram_text(c), encoding="utf-8")
        return _go(f"/clip/{cid}", "Caption saved.")
    if action == "rehook":
        if hook and hook.strip():
            _bg(pipeline.rerender, cfg, _db(cfg), cid, hook.strip())
            return _go(f"/clip/{cid}", "Re-rendering with the new hook… refresh in about a minute.")
        return _go(f"/clip/{cid}", "Hook was empty.")
    return JSONResponse({"msg": "unknown action"}, status_code=400)


# ── music ───────────────────────────────────────────────────────────────────

@app.get("/music")
def music_page(msg: str = ""):
    cfg = _cfg()
    mus.ensure_library(cfg)
    root = mus.library(cfg)
    games = '<option value="">All games</option>' + "".join(
        f'<option value="{E(k)}">{E(v.get("name", k))} only</option>' for k, v in cfg["games"].items() if k != "default")
    moods = "".join(f'<option value="{m}">{m}</option>' for m in mus.MOODS)
    rows = []
    for f in sorted(p for p in root.rglob("*") if p.suffix.lower() in mus.AUDIO_EXT):
        rel = f.relative_to(root)
        rows.append(f'<div class="row" style="align-items:center;margin:6px 0"><span style="flex:3;font-size:14px">{E(str(rel))}</span>'
                    f'<a class="btn alt" style="flex:0" onclick="act(\'/api/music/delete?path={quote(str(rel))}\',\'Delete this track?\')">🗑</a></div>')
    body = f"""<h1>🎵 Music</h1>
<div class="card"><p class="muted">Claude picks a mood for every reel and the agent lines the track's drop up with the big moment.
Only use music you have the rights to (royalty-free or licensed). No music for a mood → AI voiceover instead.</p>
<label>Mood</label><select id="mood">{moods}</select><label>For</label><select id="mgame">{games}</select>
<input type="file" id="tracks" accept="audio/*" multiple><progress id="mbar" value="0" max="100"></progress><div id="mout" class="muted"></div>
<button class="btn full" style="margin-top:10px" onclick="upload(document.getElementById('tracks'), ()=>'/api/music/upload?mood='+document.getElementById('mood').value+'&game='+document.getElementById('mgame').value, document.getElementById('mbar'), document.getElementById('mout'))">Upload tracks</button></div>
<div class="card"><h2>Library ({len(rows)})</h2>{''.join(rows) or '<p class="muted">Empty. Reels get an AI voiceover until you add tracks.</p>'}</div>"""
    return page("Music", body, "/music", msg)


@app.put("/api/music/upload")
async def music_upload(request: Request, mood: str, name: str, game: str = ""):
    cfg = _cfg()
    if mood not in mus.MOODS or (game and game not in cfg["games"]):
        return JSONResponse({"msg": "bad mood/game"}, status_code=400)
    safe = "".join(c for c in Path(name).name if c.isalnum() or c in "._- ")
    if Path(safe).suffix.lower() not in mus.AUDIO_EXT:
        return JSONResponse({"msg": "audio files only (mp3, wav, m4a, aac, ogg, flac)"}, status_code=400)
    folder = mus.library(cfg) / (game or "") / mood
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / safe, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return {"msg": "ok"}


@app.post("/api/music/delete")
def music_delete(path: str):
    root = mus.library(_cfg()).resolve()
    p = (root / path).resolve()
    if root in p.parents and p.suffix.lower() in mus.AUDIO_EXT and p.exists():
        p.unlink()
        return {"msg": "Track deleted."}
    return {"msg": "Not found."}


# ── stats / reports / log ──────────────────────────────────────────────────

@app.get("/reports")
def reports(msg: str = "", f: str = ""):
    cfg = _cfg()
    d = cfg.output / "reports"
    d.mkdir(parents=True, exist_ok=True)
    files = sorted(d.glob("*.md"), reverse=True)
    pick = next((x for x in files if x.name == f), files[0] if files else None)
    lst = " · ".join(f'<a href="/reports?f={quote(x.name)}">{E(x.stem)}</a>' for x in files[:12])
    content = E(pick.read_text(encoding="utf-8")) if pick else "No reports yet. They appear after your first posts get views."
    body = f"""<h1>📊 Stats</h1>
<div class="row" style="margin-bottom:12px"><a class="btn alt" onclick="act('/api/job/trends')">Refresh trends</a>
<a class="btn alt" onclick="act('/api/job/audit')">Run audit</a>
<a class="btn alt" onclick="act('/api/job/replies')">Reply to comments</a><a class="btn alt" onclick="act('/api/job/profile')">Profile check</a></div>
<div class="card"><h2>{E(pick.stem) if pick else "Reports"}</h2><pre>{content}</pre><p class="muted">{lst}</p></div>"""
    return page("Stats", body, "/reports", msg)


@app.post("/api/job/{name}")
def job(name: str):
    cfg = _cfg()
    if name == "trends":
        if not cfg.ai_available:
            return {"msg": "Add a Gemini (free) or Claude key in Settings first."}
        _bg(lambda: trends.refresh(cfg, log=lambda m: pipeline.log(m, cfg)))
        return {"msg": "Researching this week's trends (about a minute). The brief appears here when done."}
    if not (cfg.ig_user_id and cfg.ig_token):
        return {"msg": "Connect Instagram first."}
    ig = instagram.from_config(cfg)
    lg = lambda m: pipeline.log(m, cfg)  # noqa: E731
    if name == "audit":
        def audit_job():
            engagement.refresh_insights(cfg, _db(cfg), ig, log=lg)
            engagement.run_audit(cfg, _db(cfg), log=lg)
        _bg(audit_job)
    elif name == "replies":
        _bg(lambda: engagement.reply_round(cfg, _db(cfg), ig, log=lg))
    elif name == "profile":
        _bg(lambda: engagement.profile_check(cfg, ig, log=lg))
    else:
        return JSONResponse({"msg": "unknown job"}, status_code=400)
    return {"msg": f"Started {name}. Results show up here and in the log in a minute."}


@app.get("/log")
def log_page(msg: str = ""):
    lf = _cfg().state_dir / "agent.log"
    text = "\n".join(lf.read_text(encoding="utf-8", errors="replace").splitlines()[-250:]) if lf.exists() else ""
    return page("Log", f"<h1>Log</h1><div class='card'><pre>{E(text) or 'Empty.'}</pre></div>", "/", msg)


# ── settings & connections ──────────────────────────────────────────────────

def ai_provider(cfg) -> str:
    from . import ai
    return {"claude": "Claude (paid)", "gemini": "Gemini (free tier)", "none": "no AI (templates)"}[ai.provider(cfg)]


def _mask(v: str | None) -> str:
    return ("•••• " + v[-4:]) if v else "not set"


@app.get("/settings")
def settings_page(msg: str = ""):
    cfg = _cfg()
    s, c, v, e, rv = cfg["schedule"], cfg["clips"], cfg.get("voiceover", {}), cfg.get("engagement", {}), cfg["review"]

    def sel(name, val, opts):
        return f'<select name="{name}">' + "".join(
            f'<option value="{o}" {"selected" if str(o) == str(val) else ""}>{o}</option>' for o in opts) + "</select>"

    def chk(name, val):
        return sel(name, "yes" if val else "no", ["yes", "no"])

    connected = bool(cfg.ig_user_id and cfg.ig_token)
    body = f"""<h1>⚙️ Settings</h1>
<div class="card"><h2>Instagram</h2><p>{"✅ Connected as @" + E(os.getenv("IG_USERNAME", "")) if connected else "❌ Not connected"}</p>
<a class="btn full alt" href="/connect">{"Reconnect / test" if connected else "Connect Instagram"}</a></div>
<form method="post" action="/settings">
<div class="card"><h2>Page</h2>
<label>Your @handle (watermark)</label><input name="handle" value="{E(cfg.get('handle', ''))}">
<label>Time zone</label><input name="timezone" value="{E(cfg.get('timezone', 'America/Toronto'))}"></div>
<div class="card"><h2>Posting</h2>
<label>Post times (24h, comma separated)</label><input name="post_times" value="{E(', '.join(map(str, s['post_times'])))}">
<label>Max posts per day</label><input name="max_per_day" type="number" min="1" max="20" value="{s['max_per_day']}">
<label>I approve each reel before it posts</label>{chk("require_approval", rv.get("require_approval"))}
<label>Minimum quality to post (1-10)</label><input name="min_quality" type="number" min="1" max="10" value="{rv.get('min_quality', 6)}"></div>
<div class="card"><h2>Editing</h2>
<label>Reels per recording</label><input name="per_video" type="number" min="1" max="10" value="{c['per_video']}">
<label>Seconds per reel</label><input name="length" type="number" min="8" max="85" value="{c['length']}">
<label>"Payoff first" teaser hook</label>{chk("teaser_hook", c.get("teaser_hook", True))}
<label>Delete the original recording after reels are made (saves space)</label>{chk("delete_recordings", c.get("delete_recordings", False))}</div>
<div class="card"><h2>Reach & trends</h2>
<label>Research what's trending every day</label>{chk("trends_on", (cfg.get("trends") or {}).get("enabled", True))}
<label>Edit style</label>{sel("style", c.get("style", "auto"), ["auto", "classic", "slowmo", "meme_bar"])}
<label>Add a trending Instagram sound to each reel</label>{chk("ig_sound", (cfg.get("instagram") or {}).get("trending_audio", True))}
<label>Trial reels (show to non-followers first)</label>{sel("trial", (cfg.get("instagram") or {}).get("trial_reels", "smart"), ["smart", "always", "off"])}
<label>Post when my followers are online (from 100 followers)</label>{chk("auto_times", s.get("auto_times", True))}
<p class="muted">Posting now at: {E(", ".join(reach.post_times(cfg)))}</p></div>
<div class="card"><h2>Voiceover</h2>
<label>When</label>{sel("vo_mode", v.get("mode", "auto"), ["auto", "always", "off"])}
<label>Voice engine</label>{sel("vo_engine", v.get("engine", "piper"), ["piper", "elevenlabs", "openai"])}
<label>Voice (Piper name / ElevenLabs voice ID / OpenAI voice)</label><input name="vo_voice" value="{E(str(v.get('voice', '')))}">
<label>Add "AI voiceover" to captions</label>{chk("vo_disclose", v.get("disclose_ai_voice", True))}</div>
<div class="card"><h2>Comments</h2><label>Auto-reply to comments</label>{chk("replies", e.get("reply_to_comments", True))}
<label>Hide abusive comments</label>{chk("hide_abusive", e.get("hide_abusive", True))}</div>
<div class="card" id="keys"><h2>AI brain & keys</h2><p class="muted">Leave a key blank to keep the current one.
Now using: <b>{E(ai_provider(cfg))}</b></p>
<label>Which AI writes hooks, captions and replies</label>{sel("provider", (cfg.get("ai") or {}).get("provider", "auto"), ["auto", "gemini", "claude", "none"])}
<label>Google Gemini key (free tier, aistudio.google.com) — {_mask(os.getenv("GEMINI_API_KEY"))}</label><input name="gemini_key" type="password" autocomplete="off">
<label>YouTube Data API key (free trend data) — {_mask(os.getenv("YOUTUBE_API_KEY"))}</label><input name="youtube_key" type="password" autocomplete="off">
<label>Claude (Anthropic) API key, paid — {_mask(cfg.anthropic_key)}</label><input name="anthropic_key" type="password" autocomplete="off">
<label>ElevenLabs key (optional) — {_mask(os.getenv("ELEVENLABS_API_KEY"))}</label><input name="elevenlabs_key" type="password" autocomplete="off">
<label>OpenAI key (optional, voices) — {_mask(os.getenv("OPENAI_API_KEY"))}</label><input name="openai_key" type="password" autocomplete="off"></div>
<button class="btn full">Save settings</button></form>
<p style="margin-top:16px"><a href="/log">Log</a> · <a href="/logout">Log out</a></p>"""
    return page("Settings", body, "/settings", msg)


@app.post("/settings")
async def settings_save(request: Request):
    f = await request.form()
    cfg = _cfg()
    data = cfg.user_config()
    yes = lambda k: f.get(k) == "yes"  # noqa: E731
    try:
        times = [t.strip() for t in str(f.get("post_times", "")).split(",") if t.strip()]
        for t in times:
            h, m = map(int, t.split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
    except Exception:
        return _go("/settings", "Post times must look like 12:30, 18:00, 21:15")
    data["handle"] = str(f.get("handle", "")).strip()
    data["timezone"] = str(f.get("timezone", "America/Toronto")).strip()
    data.setdefault("schedule", {}).update(post_times=times, max_per_day=int(f.get("max_per_day", 3)))
    data.setdefault("review", {}).update(require_approval=yes("require_approval"), min_quality=int(f.get("min_quality", 6)))
    data.setdefault("clips", {}).update(per_video=int(f.get("per_video", 3)), length=int(f.get("length", 24)),
                                        teaser_hook=yes("teaser_hook"), delete_recordings=yes("delete_recordings"))
    data.setdefault("voiceover", {}).update(mode=f.get("vo_mode", "auto"), engine=f.get("vo_engine", "piper"),
                                            voice=str(f.get("vo_voice", "")).strip(), disclose_ai_voice=yes("vo_disclose"))
    data.setdefault("engagement", {}).update(reply_to_comments=yes("replies"), hide_abusive=yes("hide_abusive"))
    data.setdefault("trends", {}).update(enabled=yes("trends_on"))
    data["clips"]["style"] = f.get("style", "auto") if f.get("style") in ("auto", "classic", "slowmo", "meme_bar") else "auto"
    data.setdefault("instagram", {}).update(trending_audio=yes("ig_sound"),
                                            trial_reels=f.get("trial", "smart") if f.get("trial") in ("smart", "always", "off") else "smart")
    data["schedule"]["auto_times"] = yes("auto_times")
    Config.save_user_config(data)
    data.setdefault("ai", {})["provider"] = f.get("provider") if f.get("provider") in ("auto", "gemini", "claude", "none") else "auto"
    Config.save_user_config(data)
    keys = {"ANTHROPIC_API_KEY": f.get("anthropic_key"), "GEMINI_API_KEY": f.get("gemini_key"),
            "YOUTUBE_API_KEY": f.get("youtube_key"), "ELEVENLABS_API_KEY": f.get("elevenlabs_key"),
            "OPENAI_API_KEY": f.get("openai_key")}
    keys = {k: str(v).strip() for k, v in keys.items() if v and str(v).strip()}
    if keys:
        update_env(keys)
    autopilot.WAKE.set()
    return _go("/settings", "Saved. Changes apply from the next cycle.")


@app.get("/connect")
def connect_page(msg: str = ""):
    cfg = _cfg()
    status = ""
    if cfg.ig_user_id and cfg.ig_token:
        try:
            ig = instagram.from_config(cfg)
            a = ig.account()
            lim = (ig.publishing_limit().get("data") or [{}])[0]
            status = (f"<p>✅ @{E(a.get('username', ''))} · {a.get('followers_count', '?')} followers · "
                      f"{a.get('media_count', '?')} posts<br><span class='muted'>Publishing quota used: "
                      f"{lim.get('quota_usage', '?')} / {(lim.get('config') or {}).get('quota_total', '?')} per 24h</span></p>")
        except Exception as ex:
            status = f"<p>❌ Saved connection doesn't work: {E(str(ex))}</p>"
    steps = "".join(f"<li>{E(line.strip()[3:])}</li>" for line in instagram.SETUP_STEPS.splitlines()[1:]
                    if line.strip()[:2].rstrip(".").isdigit())
    body = f"""<h1>📸 Connect Instagram</h1>{f'<div class="card">{status}</div>' if status else ''}
<div class="card"><h2>One-time steps (~15 min)</h2><ol class="muted">{steps}</ol>
<p class="muted">Permissions to tick in Graph API Explorer: instagram_basic, instagram_content_publish, instagram_manage_comments,
instagram_manage_insights, pages_show_list, pages_read_engagement, business_management</p>
<p class="muted">Tip: open developers.facebook.com in your phone browser and use "Desktop site" if a menu is hidden.</p></div>
<div class="card"><form method="post" action="/connect">
<label>App ID</label><input name="app_id" inputmode="numeric" autocomplete="off">
<label>App Secret</label><input name="app_secret" type="password" autocomplete="off">
<label>Access token from Graph API Explorer</label><textarea name="token" style="min-height:80px"></textarea>
<button class="btn full">Connect</button></form></div>"""
    return page("Connect", body, "/settings", msg)


@app.post("/connect")
def connect(app_id: str = Form(""), app_secret: str = Form(""), token: str = Form("")):
    cfg = _cfg()
    try:
        accts = instagram.linked_accounts(cfg, app_id, app_secret, token)
    except Exception as ex:
        return _go("/connect", f"Couldn't connect: {ex}")
    if len(accts) == 1:
        info = instagram.save_account(cfg, accts[0])
        autopilot.WAKE.set()
        return _go("/", f"✅ Connected @{info.get('username')}. Autopilot will post at your next slot.")
    nonce = secrets.token_urlsafe(12)
    _pending_accounts[nonce] = accts
    opts = "".join(f'<option value="{i}">{E(a["page"])} → @{E(a["username"])}</option>' for i, a in enumerate(accts))
    body = f"""<h1>Pick the account</h1><div class="card"><form method="post" action="/connect/choose">
<input type="hidden" name="nonce" value="{nonce}"><select name="i">{opts}</select><button class="btn full">Use this one</button></form></div>"""
    return page("Connect", body, "/settings")


@app.post("/connect/choose")
def connect_choose(nonce: str = Form(""), i: int = Form(0)):
    accts = _pending_accounts.pop(nonce, None)
    if not accts:
        return _go("/connect", "That took too long, please try again.")
    info = instagram.save_account(_cfg(), accts[i])
    autopilot.WAKE.set()
    return _go("/", f"✅ Connected @{info.get('username')}.")


# ── run ─────────────────────────────────────────────────────────────────────

def serve(config_path: str | None = None, host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    global CONFIG_PATH
    CONFIG_PATH = config_path
    if not os.getenv("DASHBOARD_PASSWORD"):
        raise SystemExit(f"Set DASHBOARD_PASSWORD in {HOME / '.env'} first (it protects the dashboard).")
    cfg = _cfg()
    mus.ensure_library(cfg)
    ok, why = voiceover.engine_ready(cfg)
    pipeline.log(f"📱 Dashboard starting (voiceover engine: {why})", cfg)
    threading.Thread(target=autopilot.watch, args=(config_path, 120, False), daemon=True).start()
    uvicorn.run(app, host=host or os.getenv("HOST", "127.0.0.1"), port=port or int(os.getenv("PORT", "8080")),
                log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
