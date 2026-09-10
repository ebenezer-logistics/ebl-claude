"""Deploy the ebl.sg/claude onboarding page and installer to the EBL web server.

Copies web/ (index.html, install.ps1, version.txt, qr.png) to /var/www/ebl-site/claude/, adds the nginx location
block once if it is missing, tests and reloads nginx. Never touches any bot. Login comes from ~/.ebl/deploy.env
(HOST, USER, PASSWORD), never from this file.
"""
import os, sys, posixpath
from pathlib import Path
import paramiko

EBL_DIR = Path(os.environ.get("EBL_SECRETS_DIR", Path.home() / ".ebl"))
vals = {}
for line in (EBL_DIR / "deploy.env").read_text(encoding="utf-8-sig").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1); vals[k.strip()] = v.strip()
HOST, USER, PASSWORD = vals["HOST"], vals["USER"], vals["PASSWORD"]

HERE = Path(__file__).resolve().parent
FILES = ["index.html", "install.ps1", "version.txt", "qr.png"]
REMOTE = "/var/www/ebl-site/claude"
NGINX = "/etc/nginx/sites-enabled/ebl-site"
BLOCK = """
    # Claude at EBL: onboarding page, installer and version file (10/09/2026).
    # The installer must be served as text so PowerShell's irm returns a string.
    location = /claude/install.ps1 {
        default_type text/plain;
        add_header Cache-Control "no-cache, must-revalidate" always;
    }
    location ^~ /claude/ {
        add_header Cache-Control "no-cache, must-revalidate" always;
        try_files $uri $uri/ =404;
    }
"""


def run(c, cmd):
    _, o, e = c.exec_command(cmd, timeout=60)
    out, err = o.read().decode(), e.read().decode()
    return o.channel.recv_exit_status(), out, err


def main():
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASSWORD, timeout=30, look_for_keys=False, allow_agent=False)
    run(c, f"mkdir -p {REMOTE}")
    sftp = c.open_sftp()
    for f in FILES:
        sftp.put(str(HERE / f), posixpath.join(REMOTE, f)); print("copied", f)
    sftp.close()
    run(c, f"chown -R www-data:www-data {REMOTE} && chmod -R a+rX {REMOTE}")
    code, o, _ = run(c, f"grep -c 'location = /claude/install.ps1' {NGINX}")
    if o.strip() == "0":
        run(c, f"cp {NGINX} {NGINX}.bak-$(date +%Y%m%d%H%M%S)")
        # insert the block just before the /assets/ location, inside the https server block
        sftp = c.open_sftp()
        with sftp.open(NGINX) as fh: conf = fh.read().decode()
        anchor = "    location /assets/ {"
        assert anchor in conf, "nginx config anchor not found; not touching it"
        conf = conf.replace(anchor, BLOCK.rstrip("\n") + "\n\n" + anchor, 1)
        with sftp.open(NGINX, "w") as fh: fh.write(conf)
        sftp.close()
        print("nginx block added")
    else:
        print("nginx block already present")
    code, o, e = run(c, "nginx -t")
    print(e.strip() or o.strip())
    if code != 0:
        sys.exit("nginx config test FAILED; nothing reloaded. Restore from the .bak file.")
    code, o, e = run(c, "systemctl reload nginx && echo reloaded")
    print(o.strip() or e.strip())
    code, o, e = run(c, "curl -sI https://ebl.sg/claude/install.ps1 | grep -i 'content-type'; curl -s https://ebl.sg/claude/version.txt")
    print(o.strip())
    c.close()


if __name__ == "__main__":
    main()
