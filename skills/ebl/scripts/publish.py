"""EBL shelf tool. Sends a project's PROJECT.md (and, for work projects, its code without secrets) to the EBL
shelf on the company DigitalOcean server, and reads the shelf back.

Usage:
  py publish.py check                    is this PC set up? (settings, python, server, shelf)
  py publish.py publish <project folder> send or update a project
  py publish.py status  <project folder> what the shelf holds for this project (+ process health if declared)
  py publish.py list                     (owner) every project on the shelf
  py publish.py version                  local skill version vs the current one at ebl.sg

Settings: ~/.ebl/publish.env with HOST, USER, PASSWORD (or KEYFILE), OWNER_NAME, OWNER_EMAIL.
Never prints a secret. Never touches anything running. Refuses to upload files that look like they hold keys.
"""
import os, re, sys, json, time, posixpath, urllib.request
from pathlib import Path

VERSION = "1.2.0"
VERSION_URL = "https://ebl.sg/claude/version.txt"
SHELF = "/srv/ebl-shelf"
DEFAULT_IGNORE = {"node_modules", ".git", "__pycache__", "logs", "dist", "build", ".cache", "scratchpad-output", ".venv", "venv"}
DEFAULT_IGNORE_PREFIX = ("auth", "session", "baileys_auth", "creds")
DEFAULT_IGNORE_FILES = {"config.json", "creds.json", "fleet-bot.pid"}
DEFAULT_IGNORE_SUFFIX = (".log", ".pid", ".bak", ".har", ".env", ".pem", ".key", ".p12", ".pfx", ".sqlite", ".db")
SECRET_RX = re.compile(
    r"sk-ant-[A-Za-z0-9_\-]{10,}|pat[A-Za-z0-9]{14}\.[A-Za-z0-9]{20,}|xkeysib-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}|\"(password|passwd|apiKey|api_key|appSecret|clientSecret|token)\"\s*:\s*\"[^\"]{6,}\""
    r"|^(PASSWORD|API_KEY|SECRET|TOKEN|ANTHROPIC_API_KEY|AIRTABLE_TOKEN)\s*=\s*['\"]?[^\s'\"()_$\[]{6,}['\"]?\s*$", re.M)
# Things that make a project EBL's business even if the builder calls it personal.
# EBL-specific only. Airtable ids are NOT here: a personal Airtable project stays personal.
EBL_FINGERPRINT_RX = re.compile(r"ebl\.sg|ebenezer|\bEBL\b|8182\s?8844|infolog|cartrack|fleet-?bot", re.I)
MAX_FILE_MB = 25


def env_path():
    return Path(os.environ.get("EBL_SECRETS_DIR", Path.home() / ".ebl")) / "publish.env"


def load_env(strict=True):
    p = env_path()
    if not p.exists():
        if strict: sys.exit(f"MISSING SETTINGS: {p} does not exist. Run the install line from https://ebl.sg/claude to request your EBL space.")
        return None
    env = {}
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); env[k.strip()] = v.strip()
    for k in ("HOST", "USER", "OWNER_NAME", "OWNER_EMAIL"):
        if not env.get(k): sys.exit(f"{p} is missing the line {k}=")
    if not env.get("PASSWORD") and not env.get("KEYFILE"):
        sys.exit(f"{p} needs PASSWORD= or KEYFILE=")
    return env


def connect(env):
    import paramiko
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(hostname=env["HOST"], username=env["USER"], timeout=30, look_for_keys=False, allow_agent=False)
    if env.get("KEYFILE"): kw["key_filename"] = env["KEYFILE"]
    else: kw["password"] = env["PASSWORD"]
    c.connect(**kw); return c


def run(c, cmd):
    _, out, err = c.exec_command(cmd, timeout=120)
    o = out.read().decode(errors="replace"); e = err.read().decode(errors="replace")
    return out.channel.recv_exit_status(), o, e


def read_ignore(root: Path):
    extra = set()
    f = root / ".publishignore"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#"): extra.add(line)
    return extra


def should_skip(rel: Path, extra):
    parts = rel.parts
    for part in parts[:-1]:
        if part in DEFAULT_IGNORE or part in extra or part.lower().startswith(DEFAULT_IGNORE_PREFIX): return True
    name = parts[-1]
    if name in DEFAULT_IGNORE_FILES or name in extra: return True
    if name.lower().endswith(DEFAULT_IGNORE_SUFFIX) or name.startswith(".env"): return True
    if str(rel).replace("\\", "/") in extra: return True
    return False


def collect(root: Path):
    """Returns (files to send, skipped count, refused list, ebl fingerprint hits)."""
    extra = read_ignore(root); files = []; skipped = 0; refused = []; fingerprints = []
    for p in root.rglob("*"):
        if not p.is_file(): continue
        rel = p.relative_to(root)
        if should_skip(rel, extra): skipped += 1; continue
        if p.stat().st_size > MAX_FILE_MB * 1024 * 1024: skipped += 1; continue
        try: text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception: text = ""
        if rel.name != "PROJECT.md" and SECRET_RX.search(text):
            refused.append(str(rel)); continue
        if rel.name != "PROJECT.md" and EBL_FINGERPRINT_RX.search(text):
            fingerprints.append(str(rel))
        files.append(rel)
    return files, skipped, refused, fingerprints


def parse_project_md(text):
    fields = {}
    for m in re.finditer(r"^\|\s*([a-z ]+?)\s*\|\s*(.*?)\s*\|\s*$", text, re.M):
        fields[m.group(1).strip()] = m.group(2).strip()
    title = re.search(r"^#\s+(.+)$", text, re.M)
    fields["name"] = title.group(1).strip() if title else ""
    flags = re.search(r"^## EBL flags\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    fields["flags"] = [l.strip().lstrip("-").strip() for l in flags.group(1).splitlines() if l.strip().startswith("-")] if flags else []
    return fields


def project_id(root: Path, fields=None):
    if fields and fields.get("id"):
        if re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*$", fields["id"]): return fields["id"]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip("-").lower()


def sftp_mkdirs(sftp, path):
    parts = path.strip("/").split("/"); cur = ""
    for part in parts:
        cur += "/" + part
        try: sftp.stat(cur)
        except IOError: sftp.mkdir(cur)


def log_event(c, line):
    safe = line.replace('"', "'")
    run(c, f"echo \"$(date '+%d/%m/%Y %H:%M') {safe}\" >> {SHELF}/_events.log 2>/dev/null || true")


def do_publish(env, folder):
    root = Path(folder).resolve()
    if not root.is_dir(): sys.exit(f"Not a folder: {root}")
    pm = root / "PROJECT.md"
    if not pm.exists(): sys.exit("PROJECT.md is missing. Write it first (the skill drafts it), then publish.")
    fields = parse_project_md(pm.read_text(encoding="utf-8-sig"))
    ptype = (fields.get("type") or "").lower()
    if ptype not in ("work", "personal"):
        sys.exit("PROJECT.md needs a line  | type | work |  or  | type | personal |  in the header table.")
    files, skipped, refused, fingerprints = collect(root)
    if ptype == "personal" and fingerprints:
        print("CANNOT BE PERSONAL: these files mention EBL, its domain, its data or its systems, so this is a work project.")
        for f in fingerprints[:15]: print("   ", f)
        print("Change the type line to  | type | work |  and publish again. If you believe this is wrong, tell the EBL admin.")
        sys.exit(3)
    if ptype == "work" and refused:
        print("REFUSED: these files look like they contain a key or password. Move the secret out or add the file to .publishignore, then run again:")
        for r in refused: print("   ", r)
        sys.exit(2)
    if ptype == "personal":
        files = [Path("PROJECT.md")]; skipped = 0
    pid = project_id(root, fields)
    dest = posixpath.join(SHELF, env["USER"], pid)
    c = connect(env)
    code, o, e = run(c, f"test -d {posixpath.join(SHELF, env['USER'])} && echo ok || echo missing")
    if "ok" not in o:
        sys.exit(f"Your shelf {posixpath.join(SHELF, env['USER'])} does not exist on the server yet. Ask the EBL admin to create it.")
    sftp = c.open_sftp(); sftp_mkdirs(sftp, dest)
    n = 0
    for rel in files:
        remote = posixpath.join(dest, *rel.parts)
        sftp_mkdirs(sftp, posixpath.dirname(remote))
        sftp.put(str(root / rel), remote); n += 1
    meta = {"id": pid, "type": ptype, "owner": env["OWNER_NAME"], "email": env["OWNER_EMAIL"], "server_user": env["USER"],
            "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "from_pc": os.environ.get("COMPUTERNAME", ""),
            "files": n, "skipped": skipped, "local_path": str(root), "flags": fields.get("flags", []), "skill_version": VERSION}
    with sftp.open(posixpath.join(dest, ".published.json"), "w") as f: f.write(json.dumps(meta, indent=2))
    log_event(c, f"PUBLISH {env['USER']}/{pid} type={ptype} by {env['OWNER_NAME']} ({n} files)")
    for fl in fields.get("flags", []): log_event(c, f"FLAG {env['USER']}/{pid}: {fl}")
    sftp.close(); c.close()
    if ptype == "personal":
        print(f"Recorded {pid} on the EBL shelf as PERSONAL: {dest}")
        print("Only PROJECT.md was sent (one page: name, owner, what it is). The content stays on this PC.")
    else:
        print(f"Published {pid} to EBL shelf: {dest}")
        print(f"{n} files copied, {skipped} skipped (generated, logs, secrets folders). PROJECT.md is the record EBL reads.")
    if fields.get("flags"): print(f"{len(fields['flags'])} flag(s) noted for the owner.")


def do_status(env, folder):
    root = Path(folder).resolve()
    fields = parse_project_md((root / "PROJECT.md").read_text(encoding="utf-8-sig")) if (root / "PROJECT.md").exists() else {}
    pid = project_id(root, fields)
    dest = posixpath.join(SHELF, env["USER"], pid)
    c = connect(env)
    code, o, e = run(c, f"cat {dest}/.published.json 2>/dev/null; echo '---PM---'; cat {dest}/PROJECT.md 2>/dev/null | head -40")
    if "published_at" not in o:
        print(f"Not on the shelf yet: {dest}. Run publish first."); c.close(); return
    meta_txt, _, pm = o.partition("---PM---")
    meta = json.loads(meta_txt)
    f = parse_project_md(pm)
    print(f"{f.get('name', pid)}  ({pid})  [{meta.get('type', '?')}]")
    print(f"  owner: {meta['owner']} <{meta['email']}>   status: {f.get('status', '?')}")
    print(f"  shelf: {dest}   published: {meta['published_at']}   files: {meta['files']}")
    if meta.get("flags"): print(f"  flags: {'; '.join(meta['flags'])}")
    m = re.search(r"process\s+([A-Za-z0-9_.-]+)", f.get("runs where", ""))
    if m:
        name = m.group(1)
        code, o2, e2 = run(c, "pm2 jlist 2>/dev/null")
        try:
            procs = json.loads(o2 or "[]")
            hit = [p for p in procs if p.get("name") == name]
            if hit:
                p = hit[0]; envv = p.get("pm2_env", {})
                up = envv.get("pm_uptime"); mins = int((time.time() * 1000 - up) / 60000) if up else None
                print(f"  process {name}: {envv.get('status')}  restarts {envv.get('restart_time')}  up {mins} min  mem {p.get('monit', {}).get('memory', 0)//1048576} MB")
            else:
                print(f"  process {name}: not found under this server user (pm2 is per user)")
        except Exception:
            print(f"  process {name}: could not read pm2")
    c.close()


def do_list(env):
    c = connect(env)
    code, o, e = run(c, f"for f in $(find {SHELF} -mindepth 3 -maxdepth 3 -name PROJECT.md 2>/dev/null | sort); do echo \"=====$f\"; cat $(dirname $f)/.published.json 2>/dev/null; echo '---PM---'; head -30 $f; done")
    code, ev, e = run(c, f"grep ' FLAG ' {SHELF}/_events.log 2>/dev/null | tail -20")
    c.close()
    if not o.strip():
        print("Nothing on the shelf yet, or this user cannot read other users' shelves (only the owner account can list all)."); return
    rows = []
    for block in o.split("=====")[1:]:
        path, _, rest = block.partition("\n"); meta_txt, _, pm = rest.partition("---PM---")
        try: meta = json.loads(meta_txt)
        except Exception: meta = {}
        f = parse_project_md(pm)
        rows.append((f.get("name") or meta.get("id", "?"), meta.get("type", "?"), meta.get("owner", "?"), f.get("status", "?"),
                     f.get("runs where", "")[:36], meta.get("published_at", "?")[:16], str(len(meta.get("flags", []))) if meta.get("flags") else "",
                     path.replace(SHELF + "/", "").replace("/PROJECT.md", "")))
    hdr = ("Project", "Type", "Owner", "Status", "Runs where", "Published", "Flags", "Shelf path")
    w = [max(len(str(r[i])) for r in rows + [hdr]) for i in range(len(hdr))]
    print("  ".join(h.ljust(w[i]) for i, h in enumerate(hdr)))
    for r in rows: print("  ".join(str(r[i]).ljust(w[i]) for i in range(len(hdr))))
    print(f"\n{len(rows)} project(s) on the EBL shelf.")
    if ev.strip():
        print("\nRecent flags:"); print(ev.strip())


def do_version():
    print(f"local skill version: {VERSION}")
    try:
        with urllib.request.urlopen(VERSION_URL, timeout=10) as r:
            latest = r.read(64).decode(errors="ignore").strip()
        if not re.match(r"^\d+\.\d+\.\d+$", latest):
            print("could not read the version file at ebl.sg (unexpected content); try again later."); return
        print(f"current version at ebl.sg: {latest}")
        if latest != VERSION:
            print("UPDATE AVAILABLE: run the install line from https://ebl.sg/claude again (it keeps your settings).")
        else:
            print("Up to date.")
    except Exception as ex:
        print(f"could not check ebl.sg ({ex.__class__.__name__}); try again later.")


def do_check():
    ok = True
    print(f"skill version {VERSION}")
    try:
        import paramiko  # noqa
        print("python + paramiko: ok")
    except ImportError:
        print("paramiko: MISSING. Run:  py -m pip install paramiko"); ok = False
    env = load_env(strict=False)
    if not env:
        print(f"settings: MISSING at {env_path()}. Run the install line from https://ebl.sg/claude to request your EBL space."); return 1
    print(f"settings: ok ({env['OWNER_NAME']}, server user {env['USER']})")
    if not ok: return 1
    try:
        c = connect(env)
    except Exception as ex:
        print(f"server: CANNOT CONNECT ({ex.__class__.__name__}). Check internet, or ask the EBL admin to check your login."); return 1
    code, o, e = run(c, f"test -d {SHELF}/{env['USER']} && echo ok || echo missing")
    c.close()
    if "ok" in o: print(f"server + shelf: ok ({SHELF}/{env['USER']})")
    else: print(f"shelf: MISSING on the server. Ask the EBL admin to create {SHELF}/{env['USER']}."); return 1
    print("READY. Say 'publish to EBL' in any project folder.")
    return 0


if __name__ == "__main__":
    modes = ("check", "publish", "status", "list", "version")
    if len(sys.argv) < 2 or sys.argv[1] not in modes:
        sys.exit(__doc__)
    mode = sys.argv[1]
    if mode == "version": do_version(); sys.exit(0)
    if mode == "check": sys.exit(do_check())
    env = load_env()
    if mode == "list": do_list(env)
    else:
        if len(sys.argv) < 3: sys.exit("give the project folder")
        (do_publish if mode == "publish" else do_status)(env, sys.argv[2])
