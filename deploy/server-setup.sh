#!/bin/bash
# ════════════════════════════════════════════════════════════════════════════
#  Reel Agent: cloud server setup
#  Paste this whole file into "User data" (DigitalOcean) / "Cloud config" (Hetzner) /
#  "Paste cloud-init script" (Oracle Cloud, under Show advanced options → Management)
#  when you create an Ubuntu 24.04 server. Change ONE line first:
# ════════════════════════════════════════════════════════════════════════════
PASSWORD="CHANGE_ME"          # ← your dashboard password (letters/numbers, 10+ characters)
# ════════════════════════════════════════════════════════════════════════════

set -e
exec > /var/log/reelagent-setup.log 2>&1
export DEBIAN_FRONTEND=noninteractive
BASE=/opt/reelagent
mkdir -p $BASE/data

# swap so small servers don't run out of memory while rendering
if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile \
  && echo '/swapfile none swap sw 0 0' >> /etc/fstab; fi

apt-get update -y && apt-get install -y curl unzip python3
curl -fsSL https://get.docker.com | sh
if [ -f /etc/iptables/rules.v4 ] && grep -q REJECT /etc/iptables/rules.v4; then
  # Oracle Cloud images block everything but SSH with iptables: open web ports there
  iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
  iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
  netfilter-persistent save || iptables-save > /etc/iptables/rules.v4
else
  apt-get install -y ufw && ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
fi

IP=$(curl -4 -fsS https://api.ipify.org || hostname -I | awk '{print $1}')
DOMAIN="$(echo "$IP" | tr . -).sslip.io"
echo "$DOMAIN" > $BASE/domain

# secrets + starting config (only written the first time)
[ -f $BASE/data/.env ] || { echo "DASHBOARD_PASSWORD=$PASSWORD" > $BASE/data/.env; chmod 600 $BASE/data/.env; }
[ -f $BASE/data/config.yaml ] || printf 'handle: "@yourpage"\nclips:\n  delete_recordings: true\n' > $BASE/data/config.yaml

# HTTPS front door: https://<ip-with-dashes>.sslip.io (free certificate, renewed automatically)
cat > $BASE/Caddyfile <<EOF
$DOMAIN {
  encode gzip
  handle /setup* {
    reverse_proxy 127.0.0.1:8081
  }
  handle {
    reverse_proxy 127.0.0.1:8080
  }
  handle_errors 502 503 {
    redir * /setup 302
  }
}
http://$IP {
  redir https://$DOMAIN/setup
}
EOF
docker rm -f caddy 2>/dev/null || true
docker run -d --name caddy --restart unless-stopped --network host \
  -v $BASE/Caddyfile:/etc/caddy/Caddyfile -v caddy_data:/data -v caddy_config:/config caddy:2

# installer page at /setup: upload the agent zip from your phone (also used for updates)
cat > $BASE/installer.py <<'PY'
import hmac, io, json, os, shutil, subprocess, threading, time, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
BASE = "/opt/reelagent"
LOG = f"{BASE}/install.log"
STATE = {"busy": False}

def password():
    for line in open(f"{BASE}/data/.env"):
        if line.startswith("DASHBOARD_PASSWORD="):
            return line.split("=", 1)[1].strip()
    return ""

PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Reel Agent setup</title><style>body{font:16px -apple-system,Roboto,sans-serif;background:#0a0c11;color:#eef1f7;margin:0}
main{max-width:560px;margin:auto;padding:18px}.c{background:#141823;border:1px solid #242b3b;border-radius:16px;padding:16px;margin:12px 0}
input{width:100%;padding:12px;font-size:16px;border-radius:10px;border:1px solid #242b3b;background:#0e121b;color:#fff;margin:6px 0 12px;box-sizing:border-box}
button{width:100%;padding:14px;border:0;border-radius:12px;background:#ff3d71;color:#fff;font-size:16px;font-weight:700}
progress{width:100%}pre{white-space:pre-wrap;font-size:12px;background:#0e121b;padding:10px;border-radius:10px;max-height:40vh;overflow:auto}
.m{color:#8d97ab;font-size:14px}a{color:#7fb2ff}</style><main><h2>🎮 Reel Agent: install / update</h2>
<div class=c><p class=m>Pick the <b>reel-agent.zip</b> file from your phone, enter your dashboard password and tap Install.
Takes about 3-5 minutes the first time. Your settings, keys and reels are kept on updates.</p>
<input type=password id=pw placeholder="Dashboard password"><input type=file id=zip accept=".zip,application/zip">
<progress id=bar value=0 max=100></progress><button onclick="go()">Install / update</button></div>
<div class=c><b>Status</b><pre id=st>loading…</pre><a href="/">Open dashboard →</a></div></main>
<script>
function go(){const f=document.getElementById('zip').files[0];if(!f){alert('Choose the zip first');return}
const x=new XMLHttpRequest();x.open('PUT','/setup/upload');x.setRequestHeader('X-Password',document.getElementById('pw').value);
x.upload.onprogress=e=>{document.getElementById('bar').value=e.loaded/e.total*100};
x.onload=()=>{document.getElementById('st').textContent=x.responseText;poll()};x.send(f)}
function poll(){fetch('/setup/status').then(r=>r.json()).then(j=>{document.getElementById('st').textContent=j.log||'Not installed yet.';
if(j.busy)setTimeout(poll,4000)})}poll();
</script>"""

def install(blob):
    STATE["busy"] = True
    try:
        with open(LOG, "w") as log:
            log.write(time.strftime("%H:%M:%S ") + "unpacking…\n"); log.flush()
            z = zipfile.ZipFile(io.BytesIO(blob))
            names = z.namelist()
            root = next((n.split("/")[0] for n in names if n.endswith("/Dockerfile") and n.count("/") == 1), None)
            if not root or any(n.startswith("/") or ".." in n.split("/") for n in names):
                log.write("❌ This isn't the reel-agent.zip file.\n"); return
            tmp = f"{BASE}/unpack"
            shutil.rmtree(tmp, ignore_errors=True); z.extractall(tmp)
            shutil.rmtree(f"{BASE}/app", ignore_errors=True); shutil.move(f"{tmp}/{root}", f"{BASE}/app")
            shutil.rmtree(tmp, ignore_errors=True)
            log.write(time.strftime("%H:%M:%S ") + "building (3-5 min the first time)…\n"); log.flush()
            r = subprocess.run(["docker", "compose", "-f", f"{BASE}/app/deploy/docker-compose.yml", "up", "-d", "--build"],
                               stdout=log, stderr=subprocess.STDOUT)
            log.write(("\n✅ Installed. Open the dashboard: https://" + open(f"{BASE}/domain").read().strip() + "/\n")
                      if r.returncode == 0 else "\n❌ Build failed (see above).\n")
    except Exception as e:
        open(LOG, "a").write(f"❌ {e}\n")
    finally:
        STATE["busy"] = False

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode(); self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/setup/status"):
            txt = open(LOG).read()[-6000:] if os.path.exists(LOG) else ""
            return self._send(200, json.dumps({"busy": STATE["busy"], "log": txt}), "application/json")
        self._send(200, PAGE)
    def do_PUT(self):
        pw = password()
        if pw in ("", "CHANGE_ME"):
            return self._send(403, "Set a real password: edit DASHBOARD_PASSWORD in /opt/reelagent/data/.env")
        if not hmac.compare_digest(self.headers.get("X-Password", "").encode(), pw.encode()):
            time.sleep(2); return self._send(403, "Wrong password.")
        if STATE["busy"]:
            return self._send(409, "Already installing, wait for it to finish.")
        n = int(self.headers.get("Content-Length", "0"))
        if not 0 < n < 300_000_000:
            return self._send(400, "That file doesn't look right.")
        blob = self.rfile.read(n)
        threading.Thread(target=install, args=(blob,), daemon=True).start()
        self._send(200, "Upload received. Installing…")
    def log_message(self, *a): pass

ThreadingHTTPServer(("127.0.0.1", 8081), H).serve_forever()
PY

cat > /etc/systemd/system/reelagent-installer.service <<EOF
[Unit]
Description=Reel Agent installer page
After=network-online.target docker.service
[Service]
ExecStart=/usr/bin/python3 $BASE/installer.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now reelagent-installer
echo "READY: open http://$IP on your phone"
