"""Deploy the EBL join service to the droplet.

- copies ebl-join.py to /root/ebl-join/
- writes /root/ebl-join/config.json ONCE (never overwrites): Brevo key from ~/.ebl/BREVO-API-KEY.txt, sender address,
  Alif's DM number, the fleet bot outbox path, server address, a random admin token
- adds two nginx locations once (/api/ebl-join/ and /claude/approve/ -> 127.0.0.1:8096), tests, reloads
- starts or restarts the pm2 process ebl-join (its own process; restarting it touches no bot)
Login from ~/.ebl/deploy.env. Usage: py deploy.py [--sender you@ebl.sg]
"""
import os, sys, json, secrets, posixpath
from pathlib import Path
import paramiko

EBL_DIR = Path(os.environ.get("EBL_SECRETS_DIR", Path.home() / ".ebl"))
vals = {}
for line in (EBL_DIR / "deploy.env").read_text(encoding="utf-8-sig").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1); vals[k.strip()] = v.strip()
HOST, USER, PASSWORD = vals["HOST"], vals["USER"], vals["PASSWORD"]
SENDER = sys.argv[sys.argv.index("--sender") + 1] if "--sender" in sys.argv else "alif@ebl.sg"
HERE = Path(__file__).resolve().parent
REMOTE = "/root/ebl-join"
NGINX = "/etc/nginx/sites-enabled/ebl-site"
BLOCK = """
    # EBL join service: space requests from new Claude users + Alif's approve page (11/09/2026).
    # Own pm2 process (ebl-join) on 8096, separate from every bot.
    location ^~ /api/ebl-join/ {
        proxy_pass http://127.0.0.1:8096;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 30s;
        client_max_body_size 8k;
    }
    location ^~ /claude/approve/ {
        proxy_pass http://127.0.0.1:8096;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_read_timeout 30s;
        client_max_body_size 8k;
    }
"""


def run(c, cmd):
    _, o, e = c.exec_command(cmd, timeout=90)
    return o.channel.recv_exit_status(), o.read().decode(), e.read().decode()


def main():
    key = [l.strip() for l in (EBL_DIR / "BREVO-API-KEY.txt").read_text(encoding="utf-8-sig").splitlines() if l.strip() and not l.startswith("#")][-1]
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASSWORD, timeout=30, look_for_keys=False, allow_agent=False)
    run(c, f"mkdir -p {REMOTE}/state && mkdir -p {REMOTE}/keys && chmod 700 {REMOTE} {REMOTE}/keys")
    sftp = c.open_sftp()
    sftp.put(str(HERE / "ebl-join.py"), f"{REMOTE}/ebl-join.py"); print("copied ebl-join.py")
    code, o, e = run(c, f"test -f {REMOTE}/config.json && echo yes || echo no")
    if "yes" in o:
        print("config.json already there; left untouched")
    else:
        cfg = {"_note": "EBL join service. Never commit. brevoApiKey = Brevo (Sendinblue) transactional API. alertDm = Alif's WhatsApp.",
               "brevoApiKey": key, "senderEmail": SENDER, "senderName": "Ebenezer Logistics",
               "alertDm": "6587673656", "outboxPath": "/root/fleet-query-bot/manual-outbox.jsonl",
               "host": HOST, "publicBase": "https://ebl.sg", "adminToken": secrets.token_urlsafe(32)}
        with sftp.open(f"{REMOTE}/config.json", "w") as f: f.write(json.dumps(cfg, indent=2))
        run(c, f"chmod 600 {REMOTE}/config.json"); print(f"config.json written (sender {SENDER})")
    code, o, e = run(c, f"grep -c 'location \\^~ /api/ebl-join/' {NGINX}")
    if o.strip() == "0":
        run(c, f"mkdir -p /root/nginx-backups && cp {NGINX} /root/nginx-backups/ebl-site.bak-$(date +%Y%m%d%H%M%S)")
        with sftp.open(NGINX) as fh: conf = fh.read().decode()
        anchor = "    location /assets/ {"
        assert anchor in conf, "nginx anchor not found; not touching it"
        conf = conf.replace(anchor, BLOCK.rstrip("\n") + "\n\n" + anchor, 1)
        with sftp.open(NGINX, "w") as fh: fh.write(conf)
        print("nginx block added")
    else:
        print("nginx block already present")
    sftp.close()
    code, o, e = run(c, "nginx -t"); print((e or o).strip())
    if code != 0: sys.exit("nginx test FAILED; nothing reloaded")
    run(c, "systemctl reload nginx")
    code, o, e = run(c, "pm2 describe ebl-join >/dev/null 2>&1 && echo have || echo none")
    if "have" in o:
        run(c, "pm2 restart ebl-join --update-env"); print("ebl-join restarted")
    else:
        code, o, e = run(c, f"cd {REMOTE} && pm2 start {REMOTE}/ebl-join.py --name ebl-join --interpreter python3 && pm2 save")
        print("ebl-join started" if code == 0 else f"pm2 start failed: {e or o}")
    import time; time.sleep(2)
    code, o, e = run(c, "curl -s http://127.0.0.1:8096/api/ebl-join/health; echo; curl -s https://ebl.sg/api/ebl-join/health; echo; pm2 jlist | python3 -c \"import json,sys; [print(p['name'], p['pm2_env']['status']) for p in json.load(sys.stdin)]\"")
    print(o.strip())
    code, o, e = run(c, "pm2 logs ebl-join --lines 5 --nostream 2>/dev/null | tail -5"); print(o.strip())
    c.close()


if __name__ == "__main__":
    main()
