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
  py autosync.py off | on       pause / resume all automatic syncing on this PC
  py autosync.py status         what is paused, what was synced, when

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

HOUSE_RULES = """EBL house rules (from the /ebl skill, apply quietly; mention only if the user asks):
- This is an Ebenezer Logistics (EBL) Claude seat. Folders you work in sync to the EBL shelf automatically after each turn. Work projects go up in full minus secrets; personal or unclassified ones send one page only (name, owner, folder), never content.
- Keep a PROJECT.md in the working folder current whenever the work is a real project (a bot, automation, skill, tool, report generator, document set). Create it without being asked, from ~/.claude/skills/ebl/templates/PROJECT.md, and refresh it when what the project does, connects to, or runs on changes. Skip it for one-off chats and scratch folders.
- You decide the type line. `work` = built for EBL or touching EBL data, customers, groups, numbers, rates, systems or ebl.sg, whatever the user calls it. `personal` = the user's own life, none of the above. Say which you chose and why in one line.
- Never write a secret value into code or PROJECT.md; name it and say where it lives. One WhatsApp number per bot. Bots never speak to customers unless the owner has said so in PROJECT.md. Write down any schedule a bot runs on (time, group).
- If the user says "ebl status", "ebl list", "publish to EBL", "ebl sync off/on/now", use the /ebl skill."""


def is_owner_pc():
    """The owner's own PC (ROLE=owner in ~/.ebl/publish.env) never auto-syncs and gets no house rules.
    The owner reads the shelf; the shelf is for the people who build after them."""
    env = P.load_env(strict=False)
    return bool(env) and env.get("ROLE", "").lower() == "owner"


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
    if is_owner_pc() and not force: say("owner PC: automatic sync does not apply here (use publish to EBL, or ebl sync now)."); return "owner"
    if OFF.exists(): say("EBL sync is paused on this PC (ebl sync on to resume)."); return "paused"
    if not root.is_dir(): say(f"not a folder: {root}"); return "skip"
    why = skip_reason(root)
    if why: say(f"skipped ({why}): {root}"); return "skip"
    env = P.load_env(strict=False)
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
    too_big = len(sendable) > TOO_MANY_FILES
    full = (ptype == "work") and not too_big
    pid = P.project_id(root, fields)
    dest = posixpath.join(P.SHELF, env["USER"], pid)

    try:
        c = P.connect(env)
    except Exception as ex:
        say(f"server not reachable ({ex.__class__.__name__}); will try again later"); log(f"connect failed {ex.__class__.__name__} for {root}"); return "offline"
    try:
        code, o, e = P.run(c, f"test -d {posixpath.join(P.SHELF, env['USER'])} && echo ok || echo missing")
        if "ok" not in o: say("your shelf does not exist on the server yet; ask Alif"); log("shelf missing"); return "noshelf"
        sftp = c.open_sftp(); P.sftp_mkdirs(sftp, dest)
        n = 0
        if full:
            # remove files on the shelf that no longer exist locally? No: additive only. A rename leaves an old copy; harmless.
            for rel in sendable:
                remote = posixpath.join(dest, *rel.parts)
                P.sftp_mkdirs(sftp, posixpath.dirname(remote))
                sftp.put(str(root / rel), remote); n += 1
        note = ""
        if too_big: note = f"\n_Too many files ({len(sendable)}) to copy automatically; one page recorded. Add a .publishignore or ask Alif._\n"
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
    for folder in list(state.keys()):
        if Path(folder).is_dir():
            r = sync(folder, verbose=verbose, force=False)
            if r == "ok": done += 1
    if verbose: print(f"{done} folder(s) updated.")


# ---------- hook entry (called by Claude Code) ----------

def pythonw():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def hook():
    try: data = json.loads(sys.stdin.read() or "{}")
    except Exception: data = {}
    ev = data.get("hook_event_name", ""); cwd = data.get("cwd") or os.getcwd()
    if is_owner_pc(): return
    if ev == "SessionStart":
        if not OFF.exists(): print(HOUSE_RULES)
        return
    if ev in ("Stop", "SessionEnd"):
        if OFF.exists(): return
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            subprocess.Popen([pythonw(), str(HERE / "autosync.py"), "sync", cwd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        except Exception as ex:
            log(f"could not start background sync: {ex}")


# ---------- install / uninstall ----------

def settings_path():
    return Path.home() / ".claude" / "settings.json"


def install():
    if is_owner_pc():
        print("owner PC (ROLE=owner in publish.env): automatic sync NOT wired here. You read the shelf; it is for the people after you.")
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
    if sp.exists() and HOOK_TAG in sp.read_text(encoding="utf-8-sig").replace("\\", "/"):
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
    if is_owner_pc(): print("owner PC: automatic sync does not apply here. Use 'ebl list' to read the shelf."); return
    print(f"autosync: {'PAUSED (ebl sync on to resume)' if OFF.exists() else 'on'}")
    sp = settings_path()
    wired = sp.exists() and HOOK_TAG in sp.read_text(encoding="utf-8-sig").replace("\\", "/")
    print(f"hook in Claude Code: {'yes' if wired else 'NO (run: py autosync.py install)'}")
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
    else: sys.exit(__doc__)
