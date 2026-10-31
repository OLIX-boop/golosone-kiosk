# Golosone Kiosk - avvio con aggiornamento automatico da GitHub.
#
# Il collegamento sul desktop avvia QUESTO file (non app.py). Fa:
#   1. controlla su GitHub se c'e' una Release piu' nuova di quella installata (file VERSION)
#   2. se si': backup della versione attuale, installa la nuova, aggiorna le librerie se servono
#   3. avvia l'overlay (app.py) e lo sorveglia:
#      - esce con 0 (personale -> Esci)     -> fine
#      - esce con 3 (personale -> Aggiorna) -> cerca aggiornamenti e riavvia
#      - va in errore entro 30 s da un aggiornamento -> torna alla versione precedente
#      - va in errore piu' tardi            -> lo riavvia (max 5 volte all'ora)
# config.json, i log e i backup non vengono mai sovrascritti.
import io, json, os, re, shutil, subprocess, sys, tempfile, time, zipfile, py_compile
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
LOGFILE = os.path.join(BASE, "kiosk_log.txt")
UPDATE_EXIT = 3
KEEP_FILES = {"config.json", "kiosk_log.txt", "kiosk_log.txt.old", "test_lettura.txt",
              "calibra_log.txt", "controlli_passepartout.txt", "VERSION"}
KEEP_DIRS = {"backup", "dist", "build", "__pycache__", ".git"}
SKIP_FROM_RELEASE = {"docs", ".github"}  # non servono sulla cassa


def log(*a):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + "[avvio] " + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_cfg():
    for name in ("config.json", "config.example.json"):
        p = os.path.join(BASE, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log("config non leggibile:", name, e)
    return {}


def upd_cfg():
    u = {"enabled": True, "repo": "OLIX-boop/golosone-kiosk", "check_timeout_s": 6}
    u.update(load_cfg().get("update", {}))
    return u


def local_version():
    try:
        with open(os.path.join(BASE, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "v0"
    except Exception:
        return "v0"


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def http_get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "golosone-kiosk-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_release(u):
    data = json.loads(http_get("https://api.github.com/repos/%s/releases/latest" % u["repo"],
                               u["check_timeout_s"]))
    return data["tag_name"], data["zipball_url"]


def python_console():
    """python.exe accanto a pythonw.exe (per pip)."""
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "python.exe")
    return cand if os.path.exists(cand) else exe


def merge_missing(dst, src):
    """Aggiunge a dst le chiavi nuove di src, senza toccare quelle esistenti."""
    changed = False
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
            changed = True
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            changed = merge_missing(dst[k], v) or changed
    return changed


def copy_tree(src, dst, skip_top=()):
    for name in os.listdir(src):
        if name in skip_top:
            continue
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s):
            os.makedirs(d, exist_ok=True)
            copy_tree(s, d)
        else:
            shutil.copy2(s, d)


def make_backup(tag):
    bdir = os.path.join(BASE, "backup", "%s_%s" % (tag, time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(bdir, exist_ok=True)
    for name in os.listdir(BASE):
        if name in KEEP_DIRS:
            continue
        s = os.path.join(BASE, name)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(bdir, name))
        elif name not in ("kiosk_log.txt", "kiosk_log.txt.old"):
            shutil.copy2(s, bdir)
    # tieni solo gli ultimi 3 backup
    root = os.path.join(BASE, "backup")
    olds = sorted(os.listdir(root), key=lambda n: os.path.getmtime(os.path.join(root, n)))
    for n in olds[:-3]:
        shutil.rmtree(os.path.join(root, n), ignore_errors=True)
    return bdir


def restore_backup(bdir):
    log("ripristino della versione precedente da", os.path.basename(bdir))
    copy_tree(bdir, BASE)


def install(tag, zip_bytes):
    tmp = tempfile.mkdtemp(prefix="golosone_upd_")
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
        z.extractall(tmp)
        tops = os.listdir(tmp)
        src = os.path.join(tmp, tops[0]) if len(tops) == 1 else tmp
        # controlli prima di toccare qualsiasi cosa
        py_compile.compile(os.path.join(src, "app.py"), doraise=True)
        py_compile.compile(os.path.join(src, "avvia.py"), doraise=True)
        if not os.path.exists(os.path.join(src, "ui", "index.html")):
            raise RuntimeError("ui/index.html mancante nella release")
        with open(os.path.join(src, "config.example.json"), encoding="utf-8") as f:
            new_example = json.load(f)
        old_req = open(os.path.join(BASE, "requirements.txt"), encoding="utf-8").read() \
            if os.path.exists(os.path.join(BASE, "requirements.txt")) else ""
        new_req = open(os.path.join(src, "requirements.txt"), encoding="utf-8").read()

        bdir = make_backup(local_version())
        log("backup salvato in", os.path.basename(bdir))
        skip = SKIP_FROM_RELEASE | KEEP_FILES
        copy_tree(src, BASE, skip_top=skip)

        # impostazioni nuove aggiunte a config.json (le tue restano uguali)
        cp = os.path.join(BASE, "config.json")
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as f:
                cfg = json.load(f)
            if merge_missing(cfg, new_example):
                with open(cp, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                log("config.json: aggiunte le impostazioni nuove")

        if new_req.strip() != old_req.strip():
            log("librerie cambiate: pip install")
            r = subprocess.run([python_console(), "-m", "pip", "install", "-r",
                                os.path.join(BASE, "requirements.txt")],
                               capture_output=True, text=True, timeout=600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log("pip exit", r.returncode, (r.stdout + r.stderr)[-400:].replace("\n", " | "))

        with open(os.path.join(BASE, "VERSION"), "w", encoding="utf-8") as f:
            f.write(tag)
        log("aggiornato a", tag)
        return bdir
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_and_update():
    """Ritorna la cartella di backup se ha aggiornato, altrimenti None. Non blocca mai l'avvio."""
    u = upd_cfg()
    if not u.get("enabled", True):
        return None
    cur = local_version()
    try:
        tag, url = latest_release(u)
    except Exception as e:
        log("controllo aggiornamenti non riuscito (avvio comunque):", type(e).__name__, e)
        return None
    if vtuple(tag) <= vtuple(cur):
        log("versione installata %s: aggiornata" % cur)
        return None
    log("nuova versione disponibile: %s (installata %s)" % (tag, cur))
    try:
        return install(tag, http_get(url, 60))
    except Exception as e:
        log("aggiornamento NON installato (resta %s):" % cur, type(e).__name__, e)
        return None


def single_instance():
    if os.name != "nt":
        return True
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    single_instance.h = k32.CreateMutexW(None, False, "GolosoneKioskLauncher")
    return ctypes.get_last_error() != 183


def main():
    if not single_instance():
        log("avvio gia' in corso: esco")
        return
    log("versione", local_version())
    just_updated = check_and_update()
    crashes = []
    while True:
        t0 = time.time()
        rc = subprocess.call([sys.executable, os.path.join(BASE, "app.py")], cwd=BASE)
        alive = time.time() - t0
        if rc == 0:
            log("overlay chiuso")
            return
        if rc == UPDATE_EXIT:
            log("aggiornamento richiesto dal personale")
            just_updated = check_and_update() or just_updated
            continue
        log("overlay terminato con errore %s dopo %.0fs" % (rc, alive))
        if just_updated and alive < 30:
            restore_backup(just_updated)
            just_updated = None
            continue
        crashes = [t for t in crashes if time.time() - t < 3600] + [time.time()]
        if len(crashes) > 5:
            log("troppi errori nell'ultima ora: mi fermo")
            return
        time.sleep(3)


if __name__ == "__main__":
    main()
