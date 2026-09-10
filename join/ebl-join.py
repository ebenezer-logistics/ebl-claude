#!/usr/bin/env python3
"""EBL join service. Lets a new EBL Claude user request their space on the shelf, proves they own an @ebl.sg
mailbox (six-digit code by email), pings the EBL admin on WhatsApp with an Approve link, and, once they tap Approve,
creates their Linux user and shelf folder and hands their key to their PC exactly once.

Runs as root under pm2 (name ebl-join) on 127.0.0.1:8096, behind nginx:
    /api/ebl-join/*   -> this service (start, verify, claim, admin)
    /claude/approve/* -> this service (the approve page the admin taps)
Separate process from every bot on purpose. If it dies, nothing else notices.

Config: /root/ebl-join/config.json  (brevoApiKey, senderEmail, senderName, alertDm, outboxPath, host, publicBase, adminToken)
State:  /root/ebl-join/state/requests.json ; keys in /root/ebl-join/keys/<ticket>.json (0600, deleted on claim)
Log:    stdout (pm2 keeps it)
"""
import json, os, re, sys, time, hashlib, secrets, subprocess, html, urllib.request, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = "/root/ebl-join"
CONFIG = json.load(open(f"{BASE}/config.json"))
STATE_DIR = f"{BASE}/state"; KEYS_DIR = f"{BASE}/keys"; REQ = f"{STATE_DIR}/requests.json"
os.makedirs(STATE_DIR, exist_ok=True); os.makedirs(KEYS_DIR, mode=0o700, exist_ok=True)
SHELF = "/srv/ebl-shelf"
EMAIL_RX = re.compile(r"^[a-z0-9._-]{1,40}@ebl\.sg$")
TICKET_RX = re.compile(r"^[a-f0-9]{32}$")
CODE_TTL = 15 * 60; REQUEST_TTL = 7 * 24 * 3600; MAX_CODES_PER_HOUR = 3; MAX_ATTEMPTS = 5
PORT = 8096


def log(msg): print(time.strftime("%d/%m/%Y %H:%M:%S"), msg, flush=True)


def load():
    try: return json.load(open(REQ))
    except Exception: return {}


def save(d):
    tmp = REQ + ".tmp"; json.dump(d, open(tmp, "w"), indent=1); os.replace(tmp, REQ)


def sweep(d):
    now = time.time(); dead = [t for t, r in d.items() if now - r.get("created", now) > REQUEST_TTL]
    for t in dead:
        d.pop(t, None)
        try: os.remove(f"{KEYS_DIR}/{t}.json")
        except FileNotFoundError: pass
    return d


def h(s): return hashlib.sha256(s.encode()).hexdigest()


def send_code(email, name, code):
    body = {"sender": {"email": CONFIG["senderEmail"], "name": CONFIG.get("senderName", "Ebenezer Logistics")},
            "to": [{"email": email, "name": name}],
            "subject": f"{code} is your EBL Claude code",
            "textContent": f"Hi {name},\n\nYour code to set up Claude for EBL work is:\n\n    {code}\n\nType it into the installer on your PC. It is valid for 15 minutes.\nIf you did not ask for this, ignore this email.\n\nEbenezer Logistics"}
    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=json.dumps(body).encode(),
                                 headers={"api-key": CONFIG["brevoApiKey"], "content-type": "application/json", "accept": "application/json",
                                          "user-agent": "ebl-join/1.0 (python-urllib)"})  # Cloudflare blocks the bare urllib agent (1010)
    with urllib.request.urlopen(req, timeout=20) as r: return r.status in (200, 201, 202)


def whatsapp(text):
    """Append a DM job for the fleet bot's outbox watcher. Same path the truck QR leads use."""
    try:
        with open(CONFIG["outboxPath"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"to": CONFIG["alertDm"], "text": text}, ensure_ascii=False) + "\n")
        return True
    except Exception as ex:
        log(f"outbox write failed: {ex}"); return False


def events(line):
    try:
        with open(f"{SHELF}/_events.log", "a") as f: f.write(f"{time.strftime('%d/%m/%Y %H:%M')} {line}\n")
    except Exception: pass


def username_for(email):
    base = re.sub(r"[^a-z0-9]", "", email.split("@")[0].lower())[:14] or "user"
    if not base[0].isalpha(): base = "u" + base
    u = base; n = 1
    while subprocess.run(["id", "-u", u], capture_output=True).returncode == 0:
        n += 1; u = f"{base}{n}"
    return u


def create_user(email):
    u = username_for(email); pw = secrets.token_urlsafe(18)
    subprocess.run(["useradd", "-m", "-s", "/bin/bash", u], check=True)
    subprocess.run(["chpasswd"], input=f"{u}:{pw}".encode(), check=True)
    d = f"{SHELF}/{u}"; os.makedirs(d, exist_ok=True); subprocess.run(["chown", f"{u}:{u}", d], check=True); os.chmod(d, 0o700)
    return u, pw


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>EBL space request</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#F6F8FB;color:#12203A;margin:0}}
.w{{max-width:480px;margin:0 auto;padding:28px 18px}}h1{{color:#10305F;font-size:24px;margin:0 0 14px}}
.c{{background:#fff;border:1.5px solid #D8E1EB;border-radius:10px;padding:16px 18px;margin:0 0 18px;line-height:1.7}}
.c b{{color:#10305F}}button{{width:100%;border:0;border-radius:10px;padding:16px;font-size:17px;font-weight:600;color:#fff;margin:0 0 10px;cursor:pointer}}
.a{{background:#1B4F9C}}.d{{background:#B3261E}}.m{{color:#4A5B75;font-size:14px}}</style></head><body><div class="w">
<h1>{title}</h1><div class="c">{body}</div>{actions}<p class="m">{foot}</p></div></body></html>"""


def page(title, body, actions="", foot="Ebenezer Logistics, Claude at EBL."):
    return PAGE.format(title=title, body=body, actions=actions, foot=foot).encode()


class H(BaseHTTPRequestHandler):
    server_version = "ebl-join/1.0"

    def log_message(self, fmt, *a): pass

    def send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096: return None
        raw = self.rfile.read(n)
        if (self.headers.get("Content-Type") or "").startswith("application/x-www-form-urlencoded"):
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
        try: return json.loads(raw or b"{}")
        except Exception: return None

    # ---------------- API ----------------
    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        b = self.body_json()
        if b is None: return self.send(400, {"error": "bad body"})
        d = sweep(load())
        if p == "/api/ebl-join/start":
            name = str(b.get("name", "")).strip()[:60]; email = str(b.get("email", "")).strip().lower(); pc = str(b.get("pc", ""))[:40]; t = str(b.get("ticket", ""))
            if len(name) < 2 or not EMAIL_RX.match(email) or not TICKET_RX.match(t): return self.send(400, {"error": "Need a name, an @ebl.sg email and a valid ticket."})
            recent = [r for r in d.values() if r["email"] == email and time.time() - r.get("code_sent_at", 0) < 3600]
            if len(recent) >= MAX_CODES_PER_HOUR: return self.send(429, {"error": "Too many codes for this email in the last hour. Try later."})
            if t in d: return self.send(409, {"error": "ticket already used"})
            code = f"{secrets.randbelow(900000) + 100000}"
            try: ok = send_code(email, name, code)
            except Exception as ex: log(f"brevo failed: {ex}"); ok = False
            if not ok: return self.send(502, {"error": "Could not send the email right now. Try again in a minute."})
            d[t] = {"ticket": t, "name": name, "email": email, "pc": pc, "code_hash": h(code), "code_sent_at": time.time(), "attempts": 0,
                    "verified": False, "status": "code_sent", "created": time.time()}
            save(d); log(f"code sent to {email} ({name}, {pc})"); return self.send(200, {"ok": True})
        if p == "/api/ebl-join/verify":
            t = str(b.get("ticket", "")); code = str(b.get("code", "")).strip()
            r = d.get(t)
            if not r or r["verified"]: return self.send(404, {"error": "unknown ticket"})
            if time.time() - r["code_sent_at"] > CODE_TTL: return self.send(410, {"error": "Code expired. Ask for a new one."})
            if r["attempts"] >= MAX_ATTEMPTS: return self.send(429, {"error": "Too many wrong codes. Ask for a new one."})
            r["attempts"] += 1
            if h(code) != r["code_hash"]: save(d); return self.send(401, {"error": "Wrong code.", "left": MAX_ATTEMPTS - r["attempts"]})
            r["verified"] = True; r["status"] = "pending"; r["approval_token"] = secrets.token_urlsafe(24); r["verified_at"] = time.time(); save(d)
            link = f"{CONFIG['publicBase']}/claude/approve/{r['approval_token']}"
            whatsapp(f"🆕 EBL space request\n\n{r['name']}\n{r['email']}\nfrom PC {r['pc'] or '?'}\n\nMailbox verified. Tap to approve or deny:\n{link}")
            events(f"JOIN requested {r['email']} ({r['name']}) from {r['pc']}")
            log(f"verified {r['email']}; approval link sent to the admin"); return self.send(200, {"ok": True, "status": "pending"})
        if p == "/api/ebl-join/admin/decide":
            if not secrets.compare_digest(str(b.get("token", "")), CONFIG["adminToken"]): return self.send(403, {"error": "no"})
            r = next((x for x in d.values() if x.get("email") == b.get("email") or x.get("ticket") == b.get("ticket")), None)
            if not r or r["status"] != "pending": return self.send(404, {"error": "no pending request for that"})
            return self.send(200, self.decide(d, r, b.get("action", "")))
        if p.startswith("/claude/approve/"):
            tok = p.rsplit("/", 1)[-1]; r = next((x for x in d.values() if x.get("approval_token") == tok), None)
            if not r: return self.send(404, page("Link not valid", "This approval link is unknown or has expired."), "text/html")
            if r["status"] != "pending": return self.send(200, page("Already decided", f"This request was already <b>{html.escape(r['status'])}</b>."), "text/html")
            res = self.decide(d, r, b.get("action", ""))
            if res.get("status") == "approved":
                return self.send(200, page("Approved", f"<b>{html.escape(r['name'])}</b> now has the server user <b>{html.escape(r['user'])}</b> and a shelf folder. Their PC collects its key by itself within ten minutes and starts syncing. Nothing else to do."), "text/html")
            if res.get("status") == "denied":
                return self.send(200, page("Denied", f"Request from <b>{html.escape(r['name'])}</b> denied. Their PC will stop asking."), "text/html")
            return self.send(400, page("Nothing done", html.escape(str(res))), "text/html")
        self.send(404, {"error": "not found"})

    def decide(self, d, r, action):
        if action == "approve":
            try: u, pw = create_user(r["email"])
            except Exception as ex: log(f"create_user failed: {ex}"); return {"error": f"could not create user: {ex}"}
            key = {"HOST": CONFIG["host"], "USER": u, "PASSWORD": pw, "OWNER_NAME": r["name"], "OWNER_EMAIL": r["email"]}
            kp = f"{KEYS_DIR}/{r['ticket']}.json"
            with open(os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f: json.dump(key, f)
            r.update({"status": "approved", "user": u, "decided_at": time.time()}); save(d)
            events(f"JOIN approved {r['email']} -> user {u}"); whatsapp(f"✅ Approved {r['name']} ({r['email']}) as server user {u}. Their PC will set itself up.")
            log(f"approved {r['email']} -> {u}"); return {"status": "approved", "user": u}
        if action == "deny":
            r.update({"status": "denied", "decided_at": time.time()}); save(d)
            events(f"JOIN denied {r['email']}"); log(f"denied {r['email']}"); return {"status": "denied"}
        return {"error": "action must be approve or deny"}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); p = u.path; q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        d = sweep(load())
        if p == "/api/ebl-join/claim":
            t = q.get("ticket", ""); r = d.get(t)
            if not r or not TICKET_RX.match(t): return self.send(404, {"status": "unknown"})
            if r["status"] in ("code_sent", "pending"): return self.send(202, {"status": r["status"]})
            if r["status"] == "denied": return self.send(410, {"status": "denied"})
            if r["status"] == "claimed": return self.send(410, {"status": "claimed"})
            kp = f"{KEYS_DIR}/{t}.json"
            if not os.path.exists(kp): return self.send(410, {"status": "claimed"})
            key = json.load(open(kp)); os.remove(kp); r["status"] = "claimed"; r["claimed_at"] = time.time(); save(d)
            events(f"JOIN key collected by {r['email']} (user {r['user']})"); log(f"key collected {r['email']}")
            return self.send(200, {"status": "approved", "key": key})
        if p == "/api/ebl-join/admin/list":
            if not secrets.compare_digest(q.get("token", ""), CONFIG["adminToken"]): return self.send(403, {"error": "no"})
            rows = [{k: v for k, v in r.items() if k not in ("code_hash", "approval_token")} for r in d.values()]
            return self.send(200, {"requests": rows})
        if p.startswith("/claude/approve/"):
            tok = p.rsplit("/", 1)[-1]; r = next((x for x in d.values() if x.get("approval_token") == tok), None)
            if not r: return self.send(404, page("Link not valid", "This approval link is unknown or has expired."), "text/html")
            if r["status"] != "pending": return self.send(200, page("Already decided", f"This request was already <b>{html.escape(r['status'])}</b>."), "text/html")
            body = (f"<b>{html.escape(r['name'])}</b><br>{html.escape(r['email'])} (mailbox verified)<br>PC: {html.escape(r['pc'] or '?')}<br>"
                    f"asked {time.strftime('%d/%m/%Y %H:%M', time.localtime(r['verified_at']))}")
            actions = (f'<form method="post"><button class="a" name="action" value="approve">Approve: create their EBL space</button>'
                       f'<button class="d" name="action" value="deny">Deny</button></form>')
            return self.send(200, page("EBL space request", body, actions, "Approving creates a server user and a shelf folder for this person only. It gives no access to bots or to other people's folders."), "text/html")
        if p == "/api/ebl-join/health": return self.send(200, {"ok": True, "pending": sum(1 for r in d.values() if r["status"] == "pending")})
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    log(f"ebl-join listening on 127.0.0.1:{PORT}; alerts -> DM {CONFIG['alertDm']}; sender {CONFIG['senderEmail']}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
