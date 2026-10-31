# Golosone Kiosk Overlay - copertura semplificata per Passepartout Retail (modalita KIOSK)
# Overlay HTML a schermo intero (pywebview) + lettura/controllo di Passepartout via UI Automation.
#
#   py app.py            avvia l'overlay
#   py app.py --test     legge una volta carrello/totale/pezzi e scrive test_lettura.txt (senza overlay)
#   py app.py --calibra  stampa cosa c'e' sotto ogni tocco. Esc poi Ctrl+C per uscire
import json, os, sys, re, time, threading, queue

FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
UI_DIR = os.path.join(getattr(sys, "_MEIPASS", BASE), "ui")
_cfg_path = os.path.join(BASE, "config.json")
if not os.path.exists(_cfg_path):  # primo avvio: si parte dall'esempio (copialo in config.json e cambia il PIN)
    _cfg_path = os.path.join(BASE, "config.example.json")
with open(_cfg_path, encoding="utf-8") as f:
    CFG = json.load(f)

WIN_TITLE = "Golosone Kiosk"
state = {"connected": False, "total": "0,00", "pieces": 0, "items": [], "has_cart": False,
         "dialog": False, "dialog_title": "", "scanning": False, "busy": False,
         "scan_error": 0, "cancel_error": 0, "cancel_ok": 0, "pay_error": 0,
         "pos_active": False, "pos_status": "", "pay_result": {"id": 0, "ok": None, "msg": ""}}
pay = {"active": False, "t0": 0, "fail_msg": "", "last_click": 0}
cmds = queue.Queue()
window = None
overlay_visible = True
busy = False      # annullo/pausa in corso: overlay e lettore non vengono gestiti in automatico
hook_ok = False   # il lettore e' catturato direttamente (niente bisogno di focus)
injecting = False # stiamo scrivendo noi in Passepartout: l'aggancio lascia passare i tasti
LOGFILE = os.path.join(BASE, "kiosk_log.txt")


def log(*a):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        if os.path.exists(LOGFILE) and os.path.getsize(LOGFILE) > 2_000_000:
            os.replace(LOGFILE, LOGFILE + ".old")
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def set_busy(v):
    global busy
    busy = v
    state["busy"] = v


# ---------------------------------------------------------------- overlay
# Tutte le operazioni sulla finestra dell'overlay usano ctypes (rilascia il GIL) e chiamate
# ASINCRONE: il thread di controllo non aspetta mai la finestra -> niente blocchi reciproci.
import ctypes
from ctypes import wintypes
_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
_ov_hwnd = 0
if _user32:
    _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]


def overlay_hwnd():
    global _ov_hwnd
    try:
        if _ov_hwnd and _user32.IsWindow(_ov_hwnd):
            return _ov_hwnd
        _ov_hwnd = _user32.FindWindowW(None, WIN_TITLE) or 0
        return _ov_hwnd
    except Exception:
        return 0


def make_noactivate():
    """WS_EX_NOACTIVATE (toccare l'overlay non ruba il focus) + WS_EX_LAYERED (serve per il click-through)."""
    try:
        h = overlay_hwnd()
        if h:
            ex = _user32.GetWindowLongW(h, -20)  # GWL_EXSTYLE
            want = ex | 0x08000000
            if CFG.get("click_through", True):
                want |= 0x00080000  # WS_EX_LAYERED
            if want != ex:
                _user32.SetWindowLongW(h, -20, want)
                if want & 0x00080000:
                    _user32.SetLayeredWindowAttributes(h, 0, 255, 2)  # LWA_ALPHA, opaco
    except Exception as e:
        log("noactivate:", e)


def set_clickthrough(on):
    """Overlay visibile ma 'trasparente ai clic': il clic vero arriva a Passepartout sotto."""
    try:
        h = overlay_hwnd()
        if not h:
            return
        ex = _user32.GetWindowLongW(h, -20)
        new = (ex | 0x00080000 | 0x20) if on else (ex & ~0x20)  # WS_EX_TRANSPARENT
        if new != ex:
            _user32.SetWindowLongW(h, -20, new)
    except Exception as e:
        log("clickthrough:", e)


def keep_on_top():
    try:
        h = overlay_hwnd()
        if h:  # HWND_TOPMOST, NOSIZE|NOMOVE|NOACTIVATE|ASYNCWINDOWPOS
            _user32.SetWindowPos(h, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x4000)
    except Exception:
        pass


def set_overlay(visible):
    global overlay_visible
    if window is None or visible == overlay_visible:
        return
    overlay_visible = visible
    try:
        h = overlay_hwnd()
        if h:
            _user32.ShowWindowAsync(h, 4 if visible else 0)  # SW_SHOWNOACTIVATE / SW_HIDE
            if visible:
                keep_on_top()
    except Exception as e:
        log("overlay:", e)


# ---------------------------------------------------------------- finestre e clic (win32, veloce)
def pid_windows(pid, exclude=()):
    """Finestre top-level visibili del processo (titolo, hwnd)."""
    import win32gui, win32process
    out = []

    def cb(h, _):
        try:
            if h in exclude or not win32gui.IsWindowVisible(h):
                return True
            if win32process.GetWindowThreadProcessId(h)[1] != pid:
                return True
            l, t, r, b = win32gui.GetWindowRect(h)
            if r - l > 60 and b - t > 40:
                out.append((win32gui.GetWindowText(h), h))
        except Exception:
            pass
        return True
    win32gui.EnumWindows(cb, None)
    return out


def find_child_hwnd(pid, window_re, text):
    """Controllo figlio (con hwnd proprio) con testo esatto, dentro una finestra del processo."""
    import win32gui
    for title, top in pid_windows(pid):
        if not re.match(window_re, title, re.I):
            continue
        found = []

        def cb(h, _):
            try:
                if win32gui.IsWindowVisible(h) and (win32gui.GetWindowText(h) or "").strip().lower() == text.lower():
                    found.append(h)
            except Exception:
                pass
            return True
        try:
            win32gui.EnumChildWindows(top, cb, None)
        except Exception:
            pass
        if found:
            return found[0]
    return None


def hwnd_at(sx, sy, pid):
    import win32gui, win32con, win32process
    skip = overlay_hwnd()
    h = win32gui.GetTopWindow(0)
    while h:
        try:
            if h != skip and win32gui.IsWindowVisible(h) \
                    and win32process.GetWindowThreadProcessId(h)[1] == pid:
                l, t, r, b = win32gui.GetWindowRect(h)
                if l <= sx < r and t <= sy < b:
                    break
        except Exception:
            pass
        h = win32gui.GetWindow(h, win32con.GW_HWNDNEXT)
    if not h:
        return None
    for _ in range(30):
        cx, cy = win32gui.ScreenToClient(h, (sx, sy))
        try:
            c = win32gui.ChildWindowFromPointEx(h, (cx, cy), 0x0001 | 0x0002)
        except Exception:
            c = win32gui.ChildWindowFromPoint(h, (cx, cy))
        if not c or c == h:
            break
        h = c
    return h


def post_click_hwnd(h, sx, sy):
    import win32gui, win32con
    cx, cy = win32gui.ScreenToClient(h, (int(sx), int(sy)))
    lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
    win32gui.PostMessage(h, win32con.WM_MOUSEMOVE, 0, lp)
    win32gui.PostMessage(h, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.05)
    win32gui.PostMessage(h, win32con.WM_LBUTTONUP, 0, lp)


def click_point(sx, sy, pid, mode):
    """post = messaggi Windows diretti (overlay resta su); real = clic vero (overlay gia' nascosto)."""
    sx, sy = int(sx), int(sy)
    if mode == "real":
        from pywinauto import mouse
        mouse.click(coords=(sx, sy))
        return
    import win32gui, win32con
    h = hwnd_at(sx, sy, pid)
    if not h:
        raise RuntimeError("nessuna finestra Passepartout in %d,%d" % (sx, sy))
    cx, cy = win32gui.ScreenToClient(h, (sx, sy))
    lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
    win32gui.PostMessage(h, win32con.WM_MOUSEMOVE, 0, lp)
    win32gui.PostMessage(h, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.08)
    win32gui.PostMessage(h, win32con.WM_LBUTTONUP, 0, lp)


# ---------------------------------------------------------------- ricerca nativa (veloce)
# pywinauto.child_window scorre tutto l'albero in Python (lento, blocca la finestra).
# FindFirst di UI Automation fa la stessa ricerca in codice nativo.
_IUIA = None


def iuia():
    global _IUIA
    if _IUIA is None:
        from pywinauto.uia_defines import IUIA
        _IUIA = IUIA()
    return _IUIA


def uia_condition(d):
    u = iuia()
    dll = u.UIA_dll
    conds = []
    if d.get("auto_id") is not None:
        conds.append(u.iuia.CreatePropertyCondition(dll.UIA_AutomationIdPropertyId, d["auto_id"]))
    if d.get("title") is not None:
        conds.append(u.iuia.CreatePropertyCondition(dll.UIA_NamePropertyId, d["title"]))
    if d.get("control_type") is not None:
        conds.append(u.iuia.CreatePropertyCondition(dll.UIA_ControlTypePropertyId,
                                                    u.known_control_types[d["control_type"]]))
    if not conds:
        return u.true_condition
    c = conds[0]
    for nxt in conds[1:]:
        c = u.iuia.CreateAndCondition(c, nxt)
    return c


def uia_find(root_element, spec):
    """root_element: IUIAutomationElement. spec: dict o lista di dict (annidati). Ritorna UIAWrapper o None."""
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper
    el = root_element
    for d in (spec if isinstance(spec, list) else [spec]):
        el = el.FindFirst(iuia().tree_scope["descendants"], uia_condition(d))
        if not el:
            return None
    return UIAWrapper(UIAElementInfo(el))


def _el_visible(it):
    """Visibilita' affidabile: Passepartout lascia in memoria le schermate del conto vecchie,
    nascoste. UIA 'IsOffscreen' non lo rileva; IsWindowVisible di Windows si'
    (controlla anche tutti i genitori). Per elementi senza finestra propria si sale al genitore."""
    try:
        h = it.CurrentNativeWindowHandle
        if h:
            return bool(_user32.IsWindowVisible(h))
        walker = iuia().iuia.ControlViewWalker
        p = it
        for _ in range(15):
            p = walker.GetParentElement(p)
            if not p:
                break
            h = p.CurrentNativeWindowHandle
            if h:
                return bool(_user32.IsWindowVisible(h))
    except Exception:
        pass
    try:
        return not it.CurrentIsOffscreen
    except Exception:
        return False


def uia_find_visible(root_element, spec):
    """Tra i controlli uguali (schermata del conto attuale + vecchie nascoste) sceglie quello visibile."""
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper
    parts = spec if isinstance(spec, list) else [spec]
    el = root_element
    for d in parts[:-1]:
        el = el.FindFirst(iuia().tree_scope["descendants"], uia_condition(d))
        if not el:
            return None
    cond = uia_condition(parts[-1])
    first = el.FindFirst(iuia().tree_scope["descendants"], cond)  # veloce: si ferma al primo
    if not first:
        return None
    if _el_visible(first):
        return UIAWrapper(UIAElementInfo(first))
    arr = el.FindAll(iuia().tree_scope["descendants"], cond)  # lento: solo se il primo e' nascosto
    for i in range(arr.Length if arr else 0):
        it = arr.GetElement(i)
        if _el_visible(it):
            return UIAWrapper(UIAElementInfo(it))
    return None


def uia_from_hwnd(h):
    return iuia().iuia.ElementFromHandle(h)


# ---------------------------------------------------------------- Passepartout
class Pos:
    def __init__(self):
        from pywinauto import Desktop
        self.desk = Desktop(backend="uia")
        self.main = self.spec = None
        self.cache = {}
        self.col_idx = None
        self.cell_getter = None

    def get_main(self):
        import win32gui
        if self.main is not None:
            try:
                if win32gui.IsWindow(self.main.handle):
                    return self.main
            except Exception:
                pass
        self.cache.clear()
        self.col_idx = None
        self.main = self.spec = None
        try:
            w = self.desk.window(title_re=CFG["window_title_re"])
            if w.exists(timeout=0):
                self.main = w.wrapper_object()
                self.spec = self.desk.window(handle=self.main.handle)
        except Exception as e:
            log("finestra:", e)
        return self.main

    @property
    def pid(self):
        return self.main.element_info.process_id

    def find(self, spec, window_re=None, quiet=False):
        """Passepartout ricrea la schermata del conto (dopo annullo/pagamento): un controllo in memoria
        puo' restare 'vivo' ma nascosto e con valori vecchi. Quindi: scadenza 2 s + deve essere visibile."""
        if not spec:
            return None
        key = json.dumps([window_re, spec], sort_keys=True)
        hit = self.cache.get(key)
        if hit is not None:
            e, t = hit
            try:
                if time.time() - t < CFG.get("cache_s", 2.0) and _el_visible(e.element_info.element):
                    return e
            except Exception:
                pass
            self.cache.pop(key, None)
        try:
            if window_re:
                roots = [uia_from_hwnd(h) for t, h in pid_windows(self.pid) if re.match(window_re, t, re.I)]
            else:
                roots = [self.main.element_info.element]
            for r in roots:
                e = uia_find_visible(r, spec)
                if e is not None:
                    if not window_re:
                        self.cache[key] = (e, time.time())
                    return e
            return None
        except Exception as ex:
            if not quiet:
                log("ricerca", spec, "->", type(ex).__name__, ex)
            return None

    def reset_cache(self):
        self.cache.clear()
        self.col_idx = None

    @staticmethod
    def text(e):
        if e is None:
            return ""
        for fn in ("get_value", "window_text"):
            try:
                v = getattr(e, fn)()
                if v:
                    return str(v).strip()
            except Exception:
                pass
        return ""

    def cell_value(self, e):
        getters = [("value", lambda: e.iface_value.CurrentValue),
                   ("legacy", lambda: e.legacy_properties().get("Value"))]
        if self.cell_getter:  # prima il metodo che ha gia' funzionato
            getters.sort(key=lambda g: g[0] != self.cell_getter)
        for nm, g in getters:
            try:
                v = g()
                if v not in (None, "") and not re.fullmatch(r".+ Row \d+", str(v).strip()):
                    self.cell_getter = nm
                    return str(v).strip()
            except Exception:
                pass
        return ""

    # ---- letture
    def read_total(self):
        return self.text(self.find(CFG["controls"]["total"])) or "0,00"

    def read_pieces(self):
        m = re.search(r"(\d+)", self.text(self.find(CFG["controls"]["pieces"])))
        return int(m.group(1)) if m else 0

    def read_items(self):
        try:
            return self.read_items_fast()
        except Exception as e:
            log("lettura veloce righe non riuscita, uso quella lenta:", type(e).__name__, e)
            return self.read_items_slow()

    def read_items_fast(self):
        """Tutte le celle della griglia in UNA richiesta a Windows (CacheRequest)."""
        g = self.find(CFG["controls"]["grid"])
        if g is None:
            return []
        u = iuia()
        dll = u.UIA_dll
        cr = u.iuia.CreateCacheRequest()
        props = [dll.UIA_NamePropertyId, dll.UIA_ValueValuePropertyId, dll.UIA_LegacyIAccessibleValuePropertyId]
        for pr in props:
            cr.AddProperty(pr)
        cond = u.iuia.CreatePropertyCondition(dll.UIA_ControlTypePropertyId, u.known_control_types["DataItem"])
        arr = g.element_info.element.FindAllBuildCache(u.tree_scope["descendants"], cond, cr)
        rows = {}
        for i in range(arr.Length if arr else 0):
            it = arr.GetElement(i)
            m = re.match(r"(.+) Row (\d+)$", (it.CachedName or "").strip())
            if not m:
                continue
            val = ""
            for pr in props[1:]:
                try:
                    v = it.GetCachedPropertyValue(pr)
                except Exception:
                    v = None
                if v not in (None, "") and not re.fullmatch(r".+ Row \d+", str(v).strip()):
                    val = str(v).strip()
                    break
            rows.setdefault(int(m.group(2)), {})[m.group(1)] = val
        cols = CFG["grid_columns"]
        out = []
        for n in sorted(rows):
            r = rows[n]
            desc = r.get(cols["descrizione"], "")
            if desc:
                out.append({"desc": desc, "qta": fmt_qta(r.get(cols["qta"], "")), "tot": r.get(cols["totale"], "")})
        return out

    def read_items_slow(self):
        g = self.find(CFG["controls"]["grid"])
        if g is None:
            return []
        cols = CFG["grid_columns"]
        out = []
        try:
            rows = g.children()
            if not rows:
                return []
            if self.col_idx is None:
                names = [re.sub(r" Row \d+$", "", c.element_info.name or "") for c in rows[0].children()]
                self.col_idx = {k: names.index(v) for k, v in cols.items() if v in names}
            for row in rows[1:]:
                cells = row.children()

                def val(k):
                    i = self.col_idx.get(k, -1)
                    return self.cell_value(cells[i]) if 0 <= i < len(cells) else ""
                desc = val("descrizione")
                if desc:
                    out.append({"desc": desc, "qta": fmt_qta(val("qta")), "tot": val("totale")})
        except Exception as e:
            log("griglia:", e)
            self.reset_cache()
        return out

    def dialogs(self):
        ign = CFG.get("ignore_windows_re", "")
        return [t for t, h in pid_windows(self.pid, exclude=(self.main.handle,))
                if not (ign and re.match(ign, t))]

    # ---- popup del POS
    def window_texts(self, h):
        """Testi visibili in una finestra di dialogo (esclusi pulsanti e barra del titolo)."""
        out = []
        try:
            for e in self.desk.window(handle=h).wrapper_object().descendants():
                ct = e.element_info.control_type
                if ct in ("Button", "TitleBar", "MenuBar", "MenuItem"):
                    continue
                t = (e.element_info.name or "").strip()
                if not t and ct == "Edit":
                    t = self.text(e)
                if t and t not in out:
                    out.append(t)
        except Exception as ex:
            log("testi popup:", ex)
        return out

    def click_dialog_button(self, h, name):
        """Preme un pulsante di un popup di Passepartout. I pulsanti sono spesso controlli personalizzati
        (non 'Button'): si cercano per testo esatto e si clicca davvero, attraverso l'overlay."""
        import win32gui
        never = CFG["pos_dialogs"]["never_click_re"]
        if re.search(never, name, re.I):
            raise RuntimeError("pulsante vietato: %s" % name)
        # 1) controllo figlio con hwnd proprio e testo esatto (es. 'OK', 'Chiudi', 'Annulla')
        found = []

        def cb(c, _):
            try:
                t = (win32gui.GetWindowText(c) or "").strip()
                if win32gui.IsWindowVisible(c) and t.lower() == name.lower() and not re.search(never, t, re.I):
                    found.append(c)
            except Exception:
                pass
            return True
        try:
            win32gui.EnumChildWindows(h, cb, None)
        except Exception:
            pass
        if found:
            l, t, r, b = win32gui.GetWindowRect(found[0])
            self.do_click({"_pt": ((l + r) / 2, (t + b) / 2)}, "real")
            log("premuto '%s' sul popup (hwnd)" % name)
            return
        # 2) UI Automation: qualsiasi controllo col nome esatto (non solo Button), esclusa la barra del titolo
        win = self.desk.window(handle=h).wrapper_object()
        for e in win.descendants():
            n = (e.element_info.name or "").strip()
            if n.lower() != name.lower() or re.search(never, n, re.I):
                continue
            try:
                if e.element_info.parent.control_type == "TitleBar":
                    continue
            except Exception:
                pass
            r = e.rectangle()
            if r.width() <= 0:
                continue
            self.do_click({"_pt": ((r.left + r.right) / 2, (r.top + r.bottom) / 2)}, "real")
            log("premuto '%s' sul popup (uia %s)" % (name, e.element_info.control_type))
            return
        raise LookupError("pulsante '%s' non trovato" % name)

    def focus_code(self):
        """Porta il focus della tastiera (cioe' del lettore) sul campo 'Codice articolo'.
        Solo chiamate dirette (niente pywinauto.set_focus, che usa AttachThreadInput e puo' bloccarsi)."""
        try:
            _user32.SetForegroundWindow(self.main.handle)
        except Exception:
            pass
        c = self.find(CFG["controls"]["code_field"])
        if c is not None:
            try:
                c.element_info.element.SetFocus()
            except Exception as e:
                log("focus campo:", type(e).__name__, e)
        keep_on_top()  # l'overlay resta visivamente sopra

    def clear_code_field(self):
        c = self.find(CFG["controls"]["code_field"])
        if c is not None:
            try:
                c.set_edit_text("")
            except Exception as e:
                log("svuota campo codice:", type(e).__name__, e)

    def has_focus(self):
        try:
            fg = _user32.GetForegroundWindow()
            if not fg:
                return False
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            return pid.value == self.pid
        except Exception:
            return False

    # ---- azioni
    def scan(self, code):
        """Scrive il codice nel campo di Passepartout e preme Invio.
        metodo 'post': scrittura diretta senza focus; 'focus': porta il focus sul campo e digita (come il lettore)."""
        global injecting
        import win32gui, win32con
        c = self.find(CFG["controls"]["code_field"])
        if c is None:
            raise RuntimeError("campo codice non trovato")
        method = CFG["scanner"].get("method", "auto")
        before = (self.read_total(), self.read_pieces())
        if method in ("auto", "post"):
            try:
                c.set_edit_text(code)
                h = c.handle
                if not h:
                    raise RuntimeError("campo senza handle")
                win32gui.PostMessage(h, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0x001C0001)
                win32gui.PostMessage(h, win32con.WM_KEYUP, win32con.VK_RETURN, 0xC01C0001)
                t_end = time.time() + 0.8
                while time.time() < t_end:
                    time.sleep(0.1)
                    if (self.read_total(), self.read_pieces()) != before or self.text(c) != code:
                        log("codice inserito (diretto)")
                        return
                log("scrittura diretta non accettata")
            except Exception as e:
                log("scrittura diretta fallita:", type(e).__name__, e)
            if method == "post":
                return
        # metodo 'focus': come farebbe il lettore, ma con l'aggancio disattivato
        import keyboard
        injecting = True
        try:
            try:
                c.set_edit_text("")
            except Exception:
                pass
            self.focus_code()
            time.sleep(0.1)
            keyboard.write(code, delay=0.005)
            keyboard.press_and_release("enter")
            time.sleep(0.15)
            log("codice inserito (focus)")
        finally:
            injecting = False

    def point_of(self, target):
        if "rel" in target:
            r = self.main.rectangle()
            return r.left + target["rel"][0], r.top + target["rel"][1]
        e = self.find(target["path"], target.get("window"), quiet=True)
        if e is None:
            raise LookupError("controllo non trovato: %s %s" % (target.get("window", ""), target["path"]))
        r = e.rectangle()
        return r.left + r.width() * target.get("x", 0.5), r.top + r.height() * target.get("y", 0.5)

    def click(self, target, mode=None, timeout=0):
        t_end = time.time() + timeout
        while True:
            try:
                sx, sy = self.point_of(target)
                break
            except LookupError:
                if time.time() > t_end:
                    raise
                time.sleep(0.15)
        click_point(sx, sy, self.pid, mode or target.get("mode", CFG.get("click_mode", "post")))

    # ---- annullo: passi con verifica del risultato
    def target_point(self, tg):
        """Ritorna (x, y, hwnd_o_None) del bersaglio di un passo."""
        if "_pt" in tg:
            return tg["_pt"][0], tg["_pt"][1], None
        if "child" in tg:
            c = tg["child"]
            if re.search(CFG["pos_dialogs"]["never_click_re"], c["text"], re.I):
                raise RuntimeError("pulsante vietato")
            h = find_child_hwnd(self.pid, c["window"], c["text"])
            if not h:
                raise LookupError("'%s' non trovato in '%s'" % (c["text"], c["window"]))
            import win32gui
            l, t, r, b = win32gui.GetWindowRect(h)
            return (l + r) / 2, (t + b) / 2, h
        x, y = self.point_of(tg)
        return x, y, None

    def expect_ok(self, exp):
        if not exp:
            return True
        if "path" in exp:
            e = self.find(exp["path"], exp.get("in_window"), quiet=True)
            try:
                if e is None or not e.is_visible():
                    return False
                r = e.rectangle()
                return r.width() > 0 and r.height() > 0
            except Exception:
                return False
        titles = [t for t, h in pid_windows(self.pid)]
        if "window" in exp:
            return any(re.match(exp["window"], t, re.I) for t in titles)
        if "window_gone" in exp:
            return not any(re.match(exp["window_gone"], t, re.I) for t in titles)
        return True

    def wait_until(self, fn, timeout):
        t_end = time.time() + timeout
        while time.time() < t_end:
            try:
                if fn():
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def do_click(self, tg, mode):
        x, y, h = self.target_point(tg)
        if mode == "post" and h:
            post_click_hwnd(h, x, y)
        elif mode == "post":
            click_point(x, y, self.pid, "post")
        elif CFG.get("click_through", True):  # clic vero attraverso l'overlay (che resta visibile)
            set_clickthrough(True)
            try:
                time.sleep(0.05)
                click_point(x, y, self.pid, "real")
                time.sleep(0.1)
            finally:
                set_clickthrough(False)
        else:  # vecchio metodo: overlay nascosto per un istante
            set_overlay(False)
            ov = overlay_hwnd()
            self.wait_until(lambda: not ov or not _user32.IsWindowVisible(ov), 1.0)
            time.sleep(0.08)
            try:
                click_point(x, y, self.pid, "real")
                time.sleep(0.15)
            finally:
                set_overlay(True)

    def run_macro(self, steps, mode, done=None):
        """Esegue i passi. done(): condizione finale (carrello vuoto) -> se vera si esce subito con successo.
        Le verifiche dei singoli passi decidono solo se ripetere il clic, non il fallimento."""
        for i, st in enumerate(steps, 1):
            if done and done():
                log("annullo: carrello gia' vuoto al passo", i)
                return
            if "wait" in st:
                time.sleep(st["wait"])
                continue
            tg, exp = st["click"], st.get("expect")
            t0 = time.time()
            if not self.wait_until(lambda: self.target_point(tg), st.get("timeout", 3)):
                log("annullo passo %d: bersaglio non trovato %s" % (i, tg))
                continue  # magari il passo non serve (es. popup gia' chiuso): si prosegue
            modes = [mode] + (["real"] if mode == "post" and CFG.get("cancel_fallback_real", True) else [])
            for m in modes:
                try:
                    self.do_click(tg, m)
                except Exception as e:
                    log("annullo passo %d: clic %s fallito: %s" % (i, m, e))
                    continue
                ok = self.wait_until(lambda: self.expect_ok(exp) or (done and done()),
                                     st.get("expect_timeout", 1.2))
                log("annullo passo %d %s (%s, %.2fs)" % (i, "ok" if ok else "senza effetto visibile", m, time.time() - t0))
                if ok:
                    break


def fmt_qta(s):
    try:
        f = float(s.replace(".", "").replace(",", ".")) if "," in s else float(s)
        return str(int(f)) if f == int(f) else s
    except Exception:
        return s


# ---------------------------------------------------------------- lettore: cattura diretta
def start_scanner_hook():
    """Intercetta il lettore (che e' una 'tastiera'): il codice non va alla finestra col focus,
    lo raccogliamo noi e lo scriviamo direttamente nel campo di Passepartout."""
    global hook_ok
    import keyboard
    sc = CFG["scanner"]
    buf = []
    last = [0.0]

    def capturing():
        return (sc.get("capture", True) and overlay_visible and state["connected"]
                and not busy and not injecting)

    def on_key(ev):
        name = ev.name or ""
        is_data = name == "enter" or len(name) == 1
        if not capturing() or not is_data:
            return True
        if keyboard.is_pressed("ctrl") or keyboard.is_pressed("alt"):
            return True  # combinazioni (es. Ctrl+Alt+Q) passano
        if ev.event_type != "down":
            return False
        now = time.time()
        if now - last[0] > sc["max_gap_ms"] / 1000:
            buf.clear()
        last[0] = now
        if name == "enter":
            code = "".join(buf)
            buf.clear()
            if len(code) >= sc["min_len"]:
                cmds.put(("scan", code))
        else:
            buf.append(name)
        return False

    def on_key_listen(ev):  # solo ascolto: i tasti arrivano comunque a Passepartout
        if ev.event_type != "down" or busy:
            return
        name = ev.name or ""
        now = time.time()
        if now - last[0] > sc["max_gap_ms"] / 1000:
            buf.clear()
        last[0] = now
        if name == "enter":
            if len(buf) >= sc["min_len"]:
                cmds.put(("scanned", ("".join(buf), state["total"], state["pieces"])))
            buf.clear()
        elif len(name) == 1:
            buf.append(name)

    if sc.get("capture", False):
        keyboard.hook(on_key, suppress=True)
    else:
        keyboard.hook(on_key_listen)

    def emergency():
        log("uscita di emergenza da tastiera")
        try:
            window.destroy()
        finally:
            os._exit(0)
    keyboard.add_hotkey(CFG.get("exit_hotkey", "ctrl+alt+q"), emergency)
    hook_ok = True
    log("lettore:", "cattura diretta" if sc.get("capture", False) else "ascolto + focus su Passepartout")


# ---------------------------------------------------------------- thread di controllo
worker_ident = [None]


def watchdog():
    """Se il ciclo di controllo resta fermo > 6 s, scrive nel log DOVE e' bloccato."""
    import traceback
    reported = 0
    while True:
        time.sleep(2)
        hb = state.get("hb", 0)
        if hb and time.time() - hb > 6 and not busy and time.time() - reported > 30:
            reported = time.time()
            fr = sys._current_frames().get(worker_ident[0])
            log("ATTENZIONE: controllo fermo da %.0fs. Punto:\n%s" % (
                time.time() - hb, "".join(traceback.format_stack(fr)[-6:]) if fr else "?"))


def pos_worker():
    import comtypes
    comtypes.CoInitialize()
    worker_ident[0] = threading.get_ident()
    pos = Pos()
    pending = None
    focus_t = [0.0]
    sig = None
    last_items = 0

    while True:
        state["hb"] = time.time()
        try:
            cmd, arg = cmds.get(timeout=CFG["poll_ms"] / 1000)
        except queue.Empty:
            cmd, arg = None, None

        w = pos.get_main()
        if w is None:
            state["connected"] = False
            set_overlay(False)
            time.sleep(1)
            continue
        state["connected"] = True

        try:
            if cmd == "scanned":  # il lettore ha scritto direttamente in Passepartout
                code, bt, bp = arg
                pending = {"t0": time.time(), "before": (bt, bp)}
                state["scanning"] = True
                log("lettura vista:", code)
            elif cmd == "refocus":
                pos.focus_code()
            elif cmd == "scan" and arg:
                pending = {"t0": time.time(), "before": (state["total"], state["pieces"])}
                state["scanning"] = True
                log("scansione", arg)
                pos.scan(arg)
            elif cmd == "pay":
                log("pagamento")
                pd = CFG["pos_dialogs"]
                t_end = time.time() + 5
                while any(re.match(pd["pos_window_re"], t) or re.match(pd["error_window_re"], t)
                          for t, h in pid_windows(pos.pid, exclude=(pos.main.handle,))):
                    if time.time() > t_end:
                        break
                    time.sleep(0.2)  # aspetta che i popup del tentativo precedente siano chiusi
                try:
                    pos.click(CFG["pay"])
                    pay.update(active=True, t0=time.time(), fail_msg="")
                    state["pos_status"] = ""
                except Exception as e:
                    log("pagamento non avviato:", e)
                    state["pay_error"] = time.time()
            elif cmd == "cancel":
                do_cancel(pos)
                sig = None
        except Exception as e:
            log("errore comando", cmd, e)

        try:
            dl = handle_dialogs(pos)  # popup NON gestiti: solo questi tolgono l'overlay
            if dl and pending and handle_scan_error_dialog(pos):
                pending = None
                state["scanning"] = False
                state["scan_error"] = time.time()
                dl = handle_dialogs(pos)
            state["dialog"] = bool(dl)
            state["dialog_title"] = dl[0] if dl else ""
            if not busy:
                set_overlay(not dl)
                # il lettore scrive dove c'e' il focus: deve stare sempre su Passepartout
                try:
                    if not dl and not state["pos_active"] and not pos.has_focus() \
                            and time.time() - focus_t[0] > 1.0:
                        focus_t[0] = time.time()
                        pos.focus_code()
                except Exception as e:
                    log("focus:", type(e).__name__, e)
            total, pieces = pos.read_total(), pos.read_pieces()
            if pay["active"] and pieces == 0:
                pay_done(True, "")
            elif pay["active"] and time.time() - pay["t0"] > 180:
                pay["active"] = False
            now = time.time()
            if pieces == 0:
                state["items"] = []
                sig, last_items = (total, pieces), now
            elif (total, pieces) != sig or now - last_items > 5:  # griglia solo se qualcosa cambia
                state["items"] = pos.read_items()
                sig, last_items = (total, pieces), now
            state["total"], state["pieces"] = total, pieces
            state["has_cart"] = pieces > 0 or bool(state["items"])
            if pending:
                if (total, pieces) != pending["before"] or dl:
                    pending = None
                    state["scanning"] = False
                elif now - pending["t0"] > CFG["scanner"]["timeout_s"]:
                    pending = None
                    state["scanning"] = False
                    state["scan_error"] = now
                    log("codice non letto da Passepartout")
                    pos.clear_code_field()
        except Exception as e:
            log("errore lettura", e)


def pay_done(ok, msg):
    pay["active"] = False
    r = state["pay_result"]
    state["pay_result"] = {"id": r["id"] + 1, "ok": ok, "msg": msg}
    log("pagamento", "RIUSCITO" if ok else "FALLITO", msg)


def handle_dialogs(pos):
    """Popup del POS gestiti in automatico (overlay resta sopra). Ritorna i titoli dei popup sconosciuti."""
    pd = CFG["pos_dialogs"]
    ign = CFG.get("ignore_windows_re", "")
    pos_w = err_w = None
    unknown = []
    for title, h in pid_windows(pos.pid, exclude=(pos.main.handle,)):
        if ign and re.match(ign, title):
            continue
        if re.match(pd["pos_window_re"], title):
            pos_w = h
        elif re.match(pd["error_window_re"], title) and \
                re.search(pd["error_text_re"], " ".join(pos.window_texts(h)), re.I):
            err_w = h
        else:
            unknown.append(title)
    state["pos_active"] = bool(pos_w or err_w)
    if not state["pos_active"]:
        return unknown
    if not busy:
        keep_on_top()
    now = time.time()
    if pos_w:
        status = " ".join(pos.window_texts(pos_w))
        if status != state["pos_status"]:
            log("POS:", repr(status))
            state["pos_status"] = status
        if re.search(pd["pos_fail_re"], status, re.I) and now - pay["last_click"] > 1.5:
            pay["fail_msg"] = status
            pay["last_click"] = now
            try:
                pos.click_dialog_button(pos_w, pd["pos_close_button"])
            except Exception as e:
                log("chiusura popup POS fallita:", e)
    if err_w and now - pay["last_click"] > 1.0:
        pay["last_click"] = now
        try:
            pos.click_dialog_button(err_w, pd["error_cancel_button"])
            pay_done(False, pay["fail_msg"] or "errore POS")
        except Exception as e:
            log("annullo popup errore POS fallito:", e)
    return unknown


def handle_scan_error_dialog(pos):
    """Dopo una scansione Passepartout puo' aprire un popup di errore (articolo non trovato...).
    Se il testo e' di errore lo chiudiamo noi (OK/Chiudi) e il cliente vede il banner Golosone.
    Popup che NON sono errori (quantita', peso...) non vengono toccati."""
    se = CFG.get("scan_error_dialogs", {})
    if not se.get("enabled", True):
        return False
    pd = CFG["pos_dialogs"]
    for title, h in pid_windows(pos.pid, exclude=(pos.main.handle,)):
        if re.match(pd["pos_window_re"], title) or re.match(pd["error_window_re"], title):
            continue
        texts = " ".join(pos.window_texts(h))
        log("popup dopo scansione: %r testo=%r" % (title, texts))
        if not re.search(se.get("text_re", "non trovat|inesistent|non valid|errat|sconosciut|errore"), texts + " " + title, re.I):
            continue
        for b in se.get("buttons", ["OK", "Chiudi"]):
            try:
                pos.click_dialog_button(h, b)
                pos.clear_code_field()
                log("popup di errore lettura chiuso automaticamente")
                return True
            except LookupError:
                continue
            except Exception as e:
                log("chiusura popup errore lettura:", e)
    return False


def do_cancel(pos):
    t0 = time.time()
    set_busy(True)  # l'overlay resta visibile ("Un momento...")
    def empty():
        return pos.read_pieces() == 0  # la cache si rinnova da sola se il controllo sparisce
    try:
        try:
            pos.run_macro(CFG.get("cancel_macro", []), CFG.get("cancel_click_mode", "post"), done=empty)
        except Exception as e:
            log("annullo: errore durante i passi:", e)
        # unico criterio che conta: il carrello di Passepartout e' vuoto?
        if pos.wait_until(empty, 3):
            state["cancel_ok"] = time.time()
            log("annullo riuscito in %.1fs" % (time.time() - t0))
        else:
            state["cancel_error"] = time.time()
            log("annullo fallito: pezzi ancora", pos.read_pieces())
    finally:
        pos.reset_cache()
        set_busy(False)
        set_overlay(True)


# ---------------------------------------------------------------- API per l'HTML
class Api:
    def get_state(self):
        return state

    def scan(self, code):
        cmds.put(("scan", str(code).strip()))

    def pay(self):
        cmds.put(("pay", None))

    def refocus(self):
        cmds.put(("refocus", None))

    def get_log(self, pin):
        if str(pin) != str(CFG["staff_pin"]):
            return None
        try:
            with open(LOGFILE, encoding="utf-8") as f:
                return "".join(f.readlines()[-45:])
        except Exception as e:
            return "log non disponibile: %s" % e

    def cancel(self):
        cmds.put(("cancel", None))

    def config(self):
        return {"brand": CFG["brand"], "cancel_enabled": bool(CFG.get("cancel_macro"))}

    def staff_exit(self, pin):
        if str(pin) == str(CFG["staff_pin"]):
            def bye():
                time.sleep(0.2)
                log("uscita personale")
                try:
                    window.destroy()
                finally:
                    os._exit(0)
            threading.Thread(target=bye, daemon=True).start()
            return True
        return False

    def staff_pause(self, pin):
        if str(pin) != str(CFG["staff_pin"]):
            return False

        def run():
            set_busy(True)
            set_overlay(False)
            time.sleep(CFG["staff_pause_s"])
            set_busy(False)
            set_overlay(True)
        threading.Thread(target=run, daemon=True).start()
        return True


# ---------------------------------------------------------------- modalita diagnostica
def run_test():
    pos = Pos()
    lines = []
    p = lambda *a: (print(*a), lines.append(" ".join(str(x) for x in a)))
    if pos.get_main() is None:
        p("ERRORE: finestra Passepartout non trovata (window_title_re in config.json)")
    else:
        p("Finestra:", pos.main.window_text())
        root = pos.main.element_info.element
        for k, spec in list(CFG["controls"].items()) + [("pay", CFG["pay"]["path"])]:
            last = (spec if isinstance(spec, list) else [spec])[-1]
            arr = root.FindAll(iuia().tree_scope["descendants"], uia_condition(last))
            copies = []
            for i in range(arr.Length if arr else 0):
                it = arr.GetElement(i)
                try:
                    nm = (it.CurrentName or "")[:20]
                except Exception:
                    nm = "?"
                copies.append("%s%s" % ("VISIBILE " if _el_visible(it) else "nascosto ", repr(nm)))
            e = pos.find(spec)
            p("%-11s" % k, "OK" if e else "NON TROVATO", "| copie:", len(copies), copies)
        t0 = time.time()
        p("Totale:", pos.read_total(), "| Pezzi:", pos.read_pieces(), "| %.2fs" % (time.time() - t0))
        t0 = time.time()
        p("Seconda lettura (con cache):", pos.read_total(), pos.read_pieces(), "| %.2fs" % (time.time() - t0))
        t0 = time.time()
        try:
            items = pos.read_items_fast()
            p("Righe lette (veloce):", len(items), "| %.2fs" % (time.time() - t0))
        except Exception as e:
            p("Lettura veloce righe ERRORE:", type(e).__name__, e)
            t0 = time.time()
            items = pos.read_items_slow()
            p("Righe lette (lenta):", len(items), "| %.2fs" % (time.time() - t0))
        for i in items:
            p("   ", i)
        try:
            p("Punto pagamento:", tuple(int(v) for v in pos.point_of(CFG["pay"])))
        except Exception as e:
            p("Punto pagamento: ERRORE", e)
        p("Popup aperti:", pos.dialogs())
    with open(os.path.join(BASE, "test_lettura.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\nSalvato test_lettura.txt")


def run_calibra():
    import win32api, win32gui
    pos = Pos()
    out = open(os.path.join(BASE, "calibra_log.txt"), "a", encoding="utf-8")
    print("Tocca i pulsanti di Passepartout. Per uscire: Esc poi Ctrl+C, o chiudi la finestra.\n")
    was = False
    while True:
        down = win32api.GetAsyncKeyState(0x01) & 0x8000
        if down and not was:
            x, y = win32api.GetCursorPos()
            root = win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), 2)
            rel = ""
            if pos.get_main() is not None:
                r = pos.main.rectangle()
                rel = "rel=[%d, %d]" % (x - r.left, y - r.top)
            try:
                ei = pos.desk.from_point(x, y).element_info
                ctl = "%s | name=%r auto_id=%r" % (ei.control_type, ei.name, ei.automation_id)
            except Exception:
                ctl = "?"
            line = "schermo=(%d,%d) %s  finestra=%r  controllo: %s" % (
                x, y, rel, win32gui.GetWindowText(root), ctl)
            print(line)
            out.write(time.strftime("%H:%M:%S ") + line + "\n")
            out.flush()
        was = bool(down)
        time.sleep(0.02)


def on_start():
    time.sleep(1)
    make_noactivate()
    threading.Thread(target=pos_worker, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        start_scanner_hook()
    except Exception as e:
        log("aggancio lettore NON attivo (prova il prompt come amministratore):", e)


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    elif "--calibra" in sys.argv:
        run_calibra()
    else:
        if _user32:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = wintypes.HANDLE
            _mutex = k32.CreateMutexW(None, False, "GolosoneKioskOverlay")
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                log("gia' in esecuzione: esco")
                sys.exit(0)
        import webview
        log("avvio overlay")
        window = webview.create_window(
            WIN_TITLE, os.path.join(UI_DIR, "index.html"), js_api=Api(),
            fullscreen=True, on_top=True, frameless=True, background_color="#FBF4EA",
        )
        webview.start(on_start, gui="edgechromium", debug=CFG.get("debug", False))
