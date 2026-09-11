"""EBL autosync. Sends work to the EBL shelf on its own, no command needed.

Wired in by `install`:
  - a Claude Code hook: after every Claude turn (Stop) the current folder is synced in the background
    if it changed; at session start (SessionStart) EBL's short house rules are added to Claude's context
    so Claude keeps PROJECT.md current and decides work vs personal itself;
  - a nightly Windows task (21:30) that re-syncs every folder seen so far.

Usage:
  py autosync.py install        add the hook and the nightly task (safe to repeat)
  py autosync.py uninstall      remove them
  py autosync.py hook           (called by Claude Code; reads the hook JSON on stdin)
  py autosync.py sync <folder>  sync one folder now, quietly (used by the hook, detached)
  py autosync.py now [<folder>] sync one folder now and print what happened
  py autosync.py all            sync every folder seen so far (the nightly task)
  py autosync.py status         what is paused, what was synced, when
  py autosync.py join --name "Full Name" --email you@ebl.sg   ask for an EBL space (emails a 6-digit code)
  py autosync.py join-code 123456                             prove the mailbox; the EBL admin gets an Approve link
  py autosync.py claim          (every 10 min by the 'EBL claim' task) collect the key once approved, wire sync
  py autosync.py requests | approve <email> | deny <email>    owner PC only, fallback to the WhatsApp link

Rules: personal or unclassified folders send ONE PAGE only (name, owner, folder), never their content.
Anything that mentions EBL, ebl.sg, its bots or its systems is work and goes up in full, minus secrets.
A file named .eblignore in a folder (or any parent) means: never sync this.
"""
import os, re, sys, json, time, hashlib, posixpath, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import publish as P  # noqa: E402

EBL_DIR = Path(os.environ.get("EBL_SECRETS_DIR", Path.home() / ".ebl"))
STATE = EBL_DIR / "sync-state.json"
OFF = EBL_DIR / "sync.off"
LOG = EBL_DIR / "sync.log"
MIN_GAP_S = 300           # a folder is not re-checked more often than this after a Claude turn
TOO_MANY_FILES = 3000     # bigger than this = record a one-pager only, tell the owner
HOOK_TAG = "ebl/scripts/autosync.py"
# The person's own Claude skills are often the real work (the scripts that run their daily jobs). They live in a
# system folder the normal walk skips, so they are synced as one work project of their own, on every turn and nightly.
SKILLS_DIR = (Path.home() / ".claude" / "skills").resolve()
SCHED_DIR = (Path.home() / ".claude" / "scheduled-tasks").resolve()
# Special folders inside ~/.claude that ARE the work: synced as fixed-name work projects, never skipped.
SPECIAL = {SKILLS_DIR: ("claude-skills", "Claude skills of"), SCHED_DIR: ("claude-scheduled-tasks", "Scheduled Claude tasks of")}
SKILL_ZIP = "https://github.com/ebenezer-logistics/ebl-claude/archive/refs/heads/main.zip"

HOUSE_RULES = """EBL house rules (from the /ebl skill, apply quietly; mention only if the user asks):
- This is an Ebenezer Logistics (EBL) Claude seat. Folders you work in sync to the EBL shelf automatically after each turn. Work projects go up in full minus secrets; personal or unclassified ones send one page only (name, owner, folder), never content.
- Keep a PROJECT.md in the working folder current whenever the work is a real project (a bot, automation, skill, tool, report generator, document set). Create it without being asked, from ~/.claude/skills/ebl/templates/PROJECT.md, and refresh it when what the project does, connects to, or runs on changes. Skip it for one-off chats and scratch folders.
- You decide the type line. `work` = built for EBL or touching EBL data, customers, groups, numbers, rates, systems or ebl.sg, whatever the user calls it. `personal` = the user's own life, none of the above. Say which you chose and why in one line.
- Never write a secret value into code or PROJECT.md; name it and say where it lives. One WhatsApp number per bot. Bots never speak to customers unless the owner has said so in PROJECT.md. Write down any schedule a bot runs on (time, group).
- If the user says "ebl status", "ebl list", "publish to EBL" or "ebl sync now", use the /ebl skill. Automatic sync is company policy; do not offer ways to pause it."""


def is_owner_pc():
    """ROLE=owner in ~/.ebl/publish.env: this PC can approve, deny and list. By default it does not auto-sync
    (the shelf is for the people after the owner); add SYNC=on to the same file and it syncs like everyone else."""
    env = P.load_env(strict=False)
    return bool(env) and env.get("ROLE", "").lower() == "owner"


def owner_exempt():
    """True only for an owner PC that has NOT opted into syncing."""
    env = P.load_env(strict=False)
    return bool(env) and env.get("ROLE", "").lower() == "owner" and env.get("SYNC", "").lower() != "on"


def log(msg):
    try:
        EBL_DIR.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f: f.write(f"{time.strftime('%d/%m/%Y %H:%M:%S')} {msg}\n")
    except Exception: pass


def load_state():
    try: return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception: return {}


def save_state(s):
    EBL_DIR.mkdir(exist_ok=True); STATE.write_text(json.dumps(s, indent=1), encoding="utf-8")


# ---------- deciding whether a folder is a project we should look at ----------

def eblignored(root: Path):
    for p in [root] + list(root.parents):
        if (p / ".eblignore").exists(): return True
    return False


def looks_like_container(root: Path):
    """A folder that holds many projects (like a 'workshop' or 'Documents') is not itself a project."""
    n = 0
    try:
        for d in root.iterdir():
            if d.is_dir() and not d.name.startswith(".") and any((d / m).exists() for m in (".git", "package.json", "PROJECT.md", "SKILL.md", "pyproject.toml")):
                n += 1
                if n >= 3: return True
    except Exception: pass
    return False


def skip_reason(root: Path):
    home = Path.home().resolve()
    s = str(root).lower()
    if root == home or root.parent == root: return "home or drive root"
    if root in (home / "Desktop", home / "Documents", home / "Downloads", home / "OneDrive"): return "container folder"
    for bad in ("\\.claude\\", "\\.ebl\\", "\\appdata\\", "\\windows\\", "\\program files", "\\$recycle.bin", "\\.git\\", "\\node_modules\\"):
        if bad in s + "\\": return "system folder"
    if s.endswith(("\\.claude", "\\.ebl")): return "system folder"
    if eblignored(root): return ".eblignore present"
    if looks_like_container(root): return "container of many projects"
    return None


# ---------- collecting files fast (prunes node_modules etc. while walking) ----------

def walk(root: Path):
    extra = P.read_ignore(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if not (d in P.DEFAULT_IGNORE or d in extra or d.lower().startswith(P.DEFAULT_IGNORE_PREFIX) or d.startswith(".git"))]
        for fn in filenames:
            rel = rel_dir / fn if str(rel_dir) != "." else Path(fn)
            if P.should_skip(rel, extra): continue
            yield rel


def signature(root: Path, rels):
    h = hashlib.sha1()
    for rel in sorted(rels, key=str):
        try:
            st = (root / rel).stat(); h.update(f"{rel}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8", "ignore"))
        except Exception: pass
    return h.hexdigest()


def classify(root: Path, rels):
    """Returns (type, fields, fingerprints, refused, files_to_send)."""
    pm = root / "PROJECT.md"
    fields = P.parse_project_md(pm.read_text(encoding="utf-8-sig", errors="ignore")) if pm.exists() else {}
    declared = (fields.get("type") or "").lower()
    fingerprints, refused, sendable = [], [], []
    for rel in rels:
        p = root / rel
        try:
            if p.stat().st_size > P.MAX_FILE_MB * 1024 * 1024: continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception: continue
        if rel.name != "PROJECT.md":
            if P.SECRET_RX.search(text): refused.append(str(rel)); continue
            if P.EBL_FINGERPRINT_RX.search(text): fingerprints.append(str(rel))
        sendable.append(rel)
    if declared == "work" or (declared != "personal" and fingerprints) or (declared == "personal" and fingerprints):
        ptype = "work"
    elif declared == "personal":
        ptype = "personal"
    else:
        ptype = "unclassified"
    return ptype, fields, fingerprints, refused, sendable


def one_pager(root: Path, env, ptype, fields, note=""):
    name = fields.get("name") or root.name
    return (f"# {name}\n\n| | |\n|---|---|\n| id | {P.project_id(root, fields)} |\n| type | {ptype} |\n"
            f"| owner | {env['OWNER_NAME']}, {env['OWNER_EMAIL']} |\n| status | {fields.get('status', 'unknown')} |\n"
            f"| folder on PC | {root} |\n| last updated | {time.strftime('%d/%m/%Y')} |\n\n## What it does\n"
            f"{fields.get('_what', '') or 'No description yet (auto-recorded by /ebl; Claude will fill this in as the work continues).'}\n{note}")


# ---------- the sync itself ----------

def sync(folder, verbose=False, force=False):
    root = Path(folder).resolve()
    say = print if verbose else (lambda *a, **k: None)
    if owner_exempt() and not force: say("owner PC: automatic sync does not apply here (use publish to EBL, or ebl sync now)."); return "owner"
    if OFF.exists(): say("EBL sync is paused on this PC."); return "paused"
    if not root.is_dir(): say(f"not a folder: {root}"); return "skip"
    is_skills = root in SPECIAL
    why = None if is_skills else skip_reason(root)
    if why: say(f"skipped ({why}): {root}"); return "skip"
    env = P.load_env(strict=False)
    if not env:
        # No key yet. If a join is waiting, every Claude turn is another chance to collect it.
        if JOIN_FILE.exists(): claim(quiet=True); env = P.load_env(strict=False)
        if not env: say("no settings file yet; nothing sent"); log("no settings; skipped"); return "nosettings"
    state = load_state(); key = str(root); st = state.get(key, {})
    now = time.time()
    if not force and now - st.get("last_attempt", 0) < MIN_GAP_S: say("checked recently; skipped"); return "recent"
    st["last_attempt"] = now; state[key] = st; save_state(state)

    rels = list(walk(root))
    if not rels: say("empty folder; skipped"); return "skip"
    sig = signature(root, rels)
    if not force and sig == st.get("sig"): say("no change since last sync"); return "unchanged"
    ptype, fields, fingerprints, refused, sendable = classify(root, rels)
    if is_skills:
        # Skills and scheduled tasks written on the EBL seat are EBL work by policy; fixed names so the owner finds them.
        sid, label = SPECIAL[root]
        ptype = "work"; fields = dict(fields, name=f"{label} {env['OWNER_NAME']}", status="in use")
    too_big = len(sendable) > TOO_MANY_FILES
    full = (ptype == "work") and not too_big
    pid = SPECIAL[root][0] if is_skills else P.project_id(root, fields)
    dest = posixpath.join(P.SHELF, env["USER"], pid)

    try:
        c = P.connect(env)
    except Exception as ex:
        say(f"server not reachable ({ex.__class__.__name__}); will try again later"); log(f"connect failed {ex.__class__.__name__} for {root}"); return "offline"
    try:
        code, o, e = P.run(c, f"test -d {posixpath.join(P.SHELF, env['USER'])} && echo ok || echo missing")
        if "ok" not in o: say("your shelf does not exist on the server yet; ask the EBL admin"); log("shelf missing"); return "noshelf"
        sftp = c.open_sftp(); P.sftp_mkdirs(sftp, dest)
        n = 0
        if full:
            # remove files on the shelf that no longer exist locally? No: additive only. A rename leaves an old copy; harmless.
            for rel in sendable:
                remote = posixpath.join(dest, *rel.parts)
                P.sftp_mkdirs(sftp, posixpath.dirname(remote))
                sftp.put(str(root / rel), remote); n += 1
        note = ""
        if too_big: note = f"\n_Too many files ({len(sendable)}) to copy automatically; one page recorded. Add a .publishignore or ask the EBL admin._\n"
        if not (root / "PROJECT.md").exists() or not full:
            with sftp.open(posixpath.join(dest, "PROJECT.md"), "w") as f: f.write(one_pager(root, env, ptype, fields, note))
            if not full: n = 1
        flags = list(fields.get("flags", []))
        if (fields.get("type") or "").lower() == "personal" and fingerprints:
            flags.append(f"marked personal but mentions EBL in {len(fingerprints)} file(s); recorded as work")
        meta = {"id": pid, "type": ptype, "owner": env["OWNER_NAME"], "email": env["OWNER_EMAIL"], "server_user": env["USER"],
                "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "from_pc": os.environ.get("COMPUTERNAME", ""),
                "files": n, "held_back_secrets": refused, "local_path": str(root), "flags": flags, "auto": True,
                "skill_version": P.VERSION, "content_sent": full}
        with sftp.open(posixpath.join(dest, ".published.json"), "w") as f: f.write(json.dumps(meta, indent=2))
        P.log_event(c, f"AUTOSYNC {env['USER']}/{pid} type={ptype} by {env['OWNER_NAME']} ({n} files{', ' + str(len(refused)) + ' held back' if refused else ''})")
        for fl in flags: P.log_event(c, f"FLAG {env['USER']}/{pid}: {fl}")
        sftp.close()
    finally:
        c.close()
    st.update({"sig": sig, "last_sync": now, "id": pid, "type": ptype, "files": n}); state[key] = st; save_state(state)
    msg = f"{'synced' if full else 'recorded one page for'} {pid} [{ptype}] {n} file(s)" + (f", {len(refused)} held back (secrets)" if refused else "")
    log(msg + f" <- {root}"); say(msg)
    return "ok"


def sync_all(verbose=False):
    state = load_state(); done = 0
    folders = list(state.keys())
    for d in SPECIAL:
        if d.is_dir() and str(d) not in folders: folders.append(str(d))
    for folder in folders:
        if Path(folder).is_dir():
            r = sync(folder, verbose=verbose, force=False)
            if r == "ok": done += 1
    if verbose: print(f"{done} folder(s) updated.")
    self_update(verbose)


def _vtuple(v):
    return tuple(int(x) for x in v.split("."))


def self_update(verbose=False):
    """Nightly: if ebl.sg says a NEWER skill exists, replace this skill folder with the current one from GitHub.
    Settings, state and logs live in ~/.ebl and are not touched. Never downgrades."""
    import urllib.request, zipfile, io, shutil, tempfile
    say = print if verbose else (lambda *a, **k: None)
    try:
        with urllib.request.urlopen(P.VERSION_URL, timeout=15) as r: latest = r.read(64).decode(errors="ignore").strip()
        if not re.match(r"^\d+\.\d+\.\d+$", latest) or _vtuple(latest) <= _vtuple(P.VERSION):
            say(f"skill {P.VERSION} is current (ebl.sg says {latest or '?'})"); return
        with urllib.request.urlopen(SKILL_ZIP, timeout=60) as r: data = r.read()
        tmp = Path(tempfile.mkdtemp(prefix="ebl-upd-"))
        with zipfile.ZipFile(io.BytesIO(data)) as z: z.extractall(tmp)
        src = next(tmp.glob("ebl-claude-*/skills/ebl"))
        got = re.search(r'^VERSION = "([^"]+)"', (src / "scripts" / "publish.py").read_text(encoding="utf-8"), re.M)
        if not (src / "SKILL.md").exists() or not got or _vtuple(got.group(1)) <= _vtuple(P.VERSION):
            raise RuntimeError("download is not newer than what is installed")
        skill_dir = HERE.parent
        for item in src.iterdir():
            dest = skill_dir / item.name
            if item.is_dir():
                shutil.rmtree(dest, ignore_errors=True); shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        shutil.rmtree(tmp, ignore_errors=True)
        log(f"skill updated {P.VERSION} -> {got.group(1)}"); say(f"skill updated {P.VERSION} -> {got.group(1)}")
    except Exception as ex:
        log(f"self-update skipped: {ex.__class__.__name__}: {ex}"); say(f"self-update skipped ({ex.__class__.__name__}: {ex})")


# ---------- hook entry (called by Claude Code) ----------

def pythonw():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def hook():
    try: data = json.loads(sys.stdin.read() or "{}")
    except Exception: data = {}
    ev = data.get("hook_event_name", ""); cwd = data.get("cwd") or os.getcwd()
    if owner_exempt(): return
    if ev == "SessionStart":
        if not OFF.exists(): print(HOUSE_RULES)
        return
    if ev in ("Stop", "SessionEnd"):
        if OFF.exists(): return
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        for target in [cwd] + [str(d) for d in SPECIAL if d.is_dir()]:
            try:
                subprocess.Popen([pythonw(), str(HERE / "autosync.py"), "sync", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
            except Exception as ex:
                log(f"could not start background sync: {ex}")


# ---------- joining: request a space, prove the mailbox, collect the key once approved ----------

JOIN_BASE = "https://ebl.sg/api/ebl-join"
JOIN_FILE = EBL_DIR / "join.json"


def _post(path, payload):
    import urllib.request, urllib.error
    req = urllib.request.Request(JOIN_BASE + path, data=json.dumps(payload).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as ex:
        try: return ex.code, json.loads(ex.read() or b"{}")
        except Exception: return ex.code, {}
    except Exception as ex:
        return 0, {"error": f"no connection ({ex.__class__.__name__})"}


def join_start(name, email):
    if P.env_path().exists(): print("This PC already has an EBL settings file. Nothing to request."); return 1
    email = email.strip().lower()
    if not re.match(r"^[a-z0-9._-]+@ebl\.sg$", email): print("The email must be an @ebl.sg address."); return 1
    import secrets
    ticket = secrets.token_hex(16)
    code, r = _post("/start", {"name": name.strip(), "email": email, "pc": os.environ.get("COMPUTERNAME", ""), "ticket": ticket})
    if code != 200: print(f"Could not send the code: {r.get('error', code)}"); return 1
    EBL_DIR.mkdir(exist_ok=True)
    JOIN_FILE.write_text(json.dumps({"ticket": ticket, "name": name.strip(), "email": email, "started": time.time(), "status": "code_sent"}), encoding="utf-8")
    print(f"A six-digit code was emailed to {email}. It is valid for 15 minutes."); return 0


def join_code(code_str):
    try: j = json.loads(JOIN_FILE.read_text(encoding="utf-8"))
    except Exception: print("No request in progress on this PC. Start with: join EBL"); return 1
    code, r = _post("/verify", {"ticket": j["ticket"], "code": code_str.strip()})
    if code != 200:
        print(f"{r.get('error', 'Could not verify')}" + (f" ({r['left']} tries left)" if r.get("left") is not None else "")); return 1
    j["status"] = "pending"; JOIN_FILE.write_text(json.dumps(j), encoding="utf-8")
    tr = f'"{pythonw()}" "{HERE / "autosync.py"}" claim'
    subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "10", "/TN", "EBL claim", "/TR", tr], capture_output=True)
    print("Mailbox verified. EBL has been asked to approve your space. This PC will finish setting itself up on its own once approved. You can close this window.")
    claim(quiet=True); return 0


def claim(quiet=False):
    """Called every 10 minutes by the 'EBL claim' task until the key arrives or the request is denied."""
    say = (lambda *a: None) if quiet else print
    if P.env_path().exists():
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", "EBL claim"], capture_output=True); JOIN_FILE.unlink(missing_ok=True); say("already set up"); return 0
    try: j = json.loads(JOIN_FILE.read_text(encoding="utf-8"))
    except Exception: subprocess.run(["schtasks", "/Delete", "/F", "/TN", "EBL claim"], capture_output=True); say("no request in progress"); return 1
    import urllib.request, urllib.error
    try:
        with urllib.request.urlopen(f"{JOIN_BASE}/claim?ticket={j['ticket']}", timeout=30) as r: code, body = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as ex:
        code = ex.code
        try: body = json.loads(ex.read() or b"{}")
        except Exception: body = {}
    except Exception as ex:
        say(f"no connection ({ex.__class__.__name__}); will try again"); return 2
    if code == 202: say(f"still waiting for approval ({body.get('status')})"); return 2
    if code == 410 or code == 404:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", "EBL claim"], capture_output=True); JOIN_FILE.unlink(missing_ok=True)
        log(f"join ended: {body.get('status', code)}"); say(f"request {body.get('status', 'closed')}; nothing set up"); return 1
    if code == 200 and body.get("key"):
        k = body["key"]; EBL_DIR.mkdir(exist_ok=True)
        P.env_path().write_text("".join(f"{a}={k[a]}\n" for a in ("HOST", "USER", "PASSWORD", "OWNER_NAME", "OWNER_EMAIL")), encoding="utf-8")
        JOIN_FILE.unlink(missing_ok=True); subprocess.run(["schtasks", "/Delete", "/F", "/TN", "EBL claim"], capture_output=True)
        log(f"key collected for {k['OWNER_EMAIL']} (server user {k['USER']}); wiring automatic sync")
        if os.environ.get("EBL_SKIP_WIRING"): say("key saved (test mode: hooks not wired)"); return 0
        install(); say("EBL space ready. Automatic sync is on."); return 0
    say(f"unexpected answer {code}"); return 2


# ---------- owner side: see and decide requests from Claude (fallback to the WhatsApp link) ----------

def owner_admin(action, who=None):
    env = P.load_env(strict=False)
    if not env or env.get("ROLE", "").lower() != "owner": print("Only the owner PC can do this."); return 1
    c = P.connect(env)
    try:
        code, tok, e = P.run(c, "python3 -c \"import json;print(json.load(open('/root/ebl-join/config.json'))['adminToken'])\"")
        tok = tok.strip()
        if action == "requests":
            code, o, e = P.run(c, f"curl -s 'http://127.0.0.1:8096/api/ebl-join/admin/list?token={tok}'")
            rows = json.loads(o or "{}").get("requests", [])
            if not rows: print("No requests."); return 0
            for r in sorted(rows, key=lambda r: r.get("created", 0), reverse=True):
                print(f"{time.strftime('%d/%m %H:%M', time.localtime(r.get('created', 0)))}  {r.get('status', '?'):10s} {r.get('name', '?'):22s} {r.get('email', '?'):28s} PC {r.get('pc', '?')}  {('user ' + r['user']) if r.get('user') else ''}")
            return 0
        payload = json.dumps({"token": tok, "email": who, "action": action})
        code, o, e = P.run(c, f"curl -s -X POST http://127.0.0.1:8096/api/ebl-join/admin/decide -H 'content-type: application/json' -d '{payload}'")
        print(o.strip()); return 0
    finally:
        c.close()


# ---------- install / uninstall ----------

def settings_path():
    return Path.home() / ".claude" / "settings.json"


def hook_wired():
    """Check the PARSED settings, not the raw text: JSON stores Windows backslashes doubled, so a text search fails."""
    sp = settings_path()
    try: s = json.loads(sp.read_text(encoding="utf-8-sig"))
    except Exception: return False
    return any(HOOK_TAG in h.get("command", "").replace("\\", "/") for groups in s.get("hooks", {}).values() for g in groups for h in g.get("hooks", []))


def install():
    if owner_exempt():
        print("owner PC (ROLE=owner in publish.env): automatic sync NOT wired here. Add SYNC=on to that file to sync this PC too.")
        uninstall(quiet=True); return
    sp = settings_path(); sp.parent.mkdir(exist_ok=True)
    try: s = json.loads(sp.read_text(encoding="utf-8-sig")) if sp.exists() else {}
    except Exception: sys.exit(f"{sp} is not valid JSON; fix it first")
    cmd = f'py "{HERE / "autosync.py"}" hook'
    hooks = s.setdefault("hooks", {})
    for ev in ("SessionStart", "Stop"):
        groups = hooks.setdefault(ev, [])
        if any(HOOK_TAG in h.get("command", "").replace("\\", "/") for g in groups for h in g.get("hooks", [])): continue
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 20}]})
    sp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    print(f"hook: ok ({sp})")
    tr = f'"{pythonw()}" "{HERE / "autosync.py"}" all'
    r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "21:30", "/TN", "EBL autosync", "/TR", tr], capture_output=True, text=True)
    print("nightly task: ok (21:30)" if r.returncode == 0 else f"nightly task: could not create ({r.stderr.strip() or r.stdout.strip()})")
    if OFF.exists(): OFF.unlink()
    print("autosync: on")


def uninstall(quiet=False):
    sp = settings_path()
    if hook_wired():
        try:
            s = json.loads(sp.read_text(encoding="utf-8-sig")); hooks = s.get("hooks", {})
            for ev in list(hooks.keys()):
                hooks[ev] = [g for g in hooks[ev] if not any(HOOK_TAG in h.get("command", "").replace("\\", "/") for h in g.get("hooks", []))]
                if not hooks[ev]: del hooks[ev]
            if not hooks: s.pop("hooks", None)
            sp.write_text(json.dumps(s, indent=2), encoding="utf-8")
            if not quiet: print("hook removed")
        except Exception as ex: print(f"could not edit {sp}: {ex}")
    r = subprocess.run(["schtasks", "/Query", "/TN", "EBL autosync"], capture_output=True)
    if r.returncode == 0:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", "EBL autosync"], capture_output=True)
        if not quiet: print("nightly task removed")


def status():
    if owner_exempt(): print("owner PC: automatic sync does not apply here (SYNC=on in publish.env would turn it on). Use 'ebl list' to read the shelf."); return
    print(f"autosync: {'PAUSED' if OFF.exists() else 'on'}" + ("  (owner PC, SYNC=on)" if is_owner_pc() else ""))
    print(f"hook in Claude Code: {'yes' if hook_wired() else 'NO (run: py autosync.py install)'}")
    r = subprocess.run(["schtasks", "/Query", "/TN", "EBL autosync"], capture_output=True, text=True)
    print(f"nightly task: {'yes' if r.returncode == 0 else 'NO'}")
    state = load_state()
    if not state: print("nothing synced yet"); return
    print("folders seen:")
    for k, v in sorted(state.items(), key=lambda kv: -kv[1].get("last_sync", 0)):
        when = time.strftime("%d/%m %H:%M", time.localtime(v["last_sync"])) if v.get("last_sync") else "never"
        print(f"  {when}  {v.get('type', '?'):12s} {v.get('id', '?'):30s} {k}")


if __name__ == "__main__":
    a = sys.argv[1:] or ["status"]
    m = a[0]
    if m == "hook": hook()
    elif m == "sync": sync(a[1] if len(a) > 1 else os.getcwd())
    elif m == "now": sync(a[1] if len(a) > 1 else os.getcwd(), verbose=True, force=True)
    elif m == "all": sync_all(verbose=True)
    elif m == "install": install()
    elif m == "uninstall": uninstall()
    elif m == "off": EBL_DIR.mkdir(exist_ok=True); OFF.write_text("paused\n"); print("autosync paused on this PC")
    elif m == "on": OFF.unlink(missing_ok=True); print("autosync on")
    elif m == "status": status()
    elif m == "join":
        name = a[a.index("--name") + 1] if "--name" in a else ""; email = a[a.index("--email") + 1] if "--email" in a else ""
        if not name or not email: sys.exit("usage: autosync.py join --name \"Full Name\" --email you@ebl.sg")
        sys.exit(join_start(name, email))
    elif m == "join-code": sys.exit(join_code(a[1] if len(a) > 1 else ""))
    elif m == "claim": sys.exit(claim(quiet=len(a) > 1 and a[1] == "quiet"))
    elif m == "requests": sys.exit(owner_admin("requests"))
    elif m in ("approve", "deny"):
        if len(a) < 2: sys.exit(f"usage: autosync.py {m} someone@ebl.sg")
        sys.exit(owner_admin(m, a[1].strip().lower()))
    else: sys.exit(__doc__)
