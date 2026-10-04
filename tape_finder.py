# -*- coding: utf-8 -*-
"""
تپه‌یاب — جستجوی خودکار کد تپه در «ستاره جنوب» و «مروارید»

وقتی در برنامه‌ی ثبت سفارش کدی مثل G12 یا B305 تایپ می‌کنید، این برنامه خودکار
در لیست تپه‌های دو برنامه (API شما) جستجو می‌کند و نتیجه را در یک پنجره‌ی
کوچک شناور نشان می‌دهد. هیچ دسترسی‌ای به کد برنامه‌ی ثبت سفارش لازم نیست.

روش کار: یک هوک سراسری کیبورد (بدون نیاز به ادمین) کلیدهای تایپ‌شده را
فقط بر اساس «کد کلید» می‌خواند (مستقل از زبان کیبورد فارسی/انگلیسی) و اگر
رشته مثل  G123  یا  B45  شد جستجو می‌کند. فقط حرف G و B و عدد نگه داشته می‌شود؛
چیز دیگری ذخیره یا ارسال نمی‌شود.
"""
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request

IS_WIN = sys.platform == "win32"

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TapeFinder")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "base_url": "https://mahroch.ir/tape-khash-api/index.php",
    "username": "",
    "remember_token": "",
    "target_process": "",        # مثلاً order.exe ؛ خالی = همه‌ی برنامه‌ها
    "refresh_minutes": 5,
    "overlay_seconds": 7,
    "enabled": True,
    "auto_names": False,         # تشخیص آزمایشی از روی نام گروه‌های فرم (UIA)
    "calib": {},                 # کادرهای معرفی‌شده: tape / sleeve / bound
}

# هر منبع: (کلید، نام نمایشی، اکشن دریافت داده)
SOURCES = [
    ("star", "ستاره جنوب", "getInitialData"),
    ("morvarid", "مروارید", "getInitialData2"),
]

CODE_RE = re.compile(r"^([GB])(\d{1,7})$")
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


# ----------------------------------------------------------------------------
# تنظیمات
# ----------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------------
# ارتباط با API
# ----------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = ""

    def _post(self, action, body=None, auth=True, timeout=25):
        url = "%s?action=%s" % (self.cfg["base_url"], action)
        data = json.dumps(body or {}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read().decode("utf-8", "replace")
                status = r.status
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            status = e.code
        except (urllib.error.URLError, OSError) as e:
            raise ApiError("خطای اتصال به سرور: %s" % e)
        try:
            js = json.loads(payload)
        except ValueError:
            raise ApiError("پاسخ نامعتبر از سرور (کد %s)" % status, status)
        if not js.get("ok"):
            raise ApiError(js.get("error") or "خطای ناشناخته", status)
        return js

    def login(self, username, password):
        js = self._post("login", {"username": username, "password": password,
                                  "remember": True}, auth=False)
        self.token = js["token"]
        self.cfg["username"] = username
        self.cfg["remember_token"] = js.get("remember_token", "")
        save_config(self.cfg)

    def login_with_remember(self):
        rt = self.cfg.get("remember_token")
        if not rt:
            raise ApiError("ابتدا وارد شوید.", 401)
        js = self._post("loginWithRememberToken", {"remember_token": rt}, auth=False)
        self.token = js["token"]

    def call_authed(self, action):
        """اگر نشست منقضی شده بود یک‌بار با remember-token دوباره وارد می‌شود."""
        if not self.token:
            self.login_with_remember()
        try:
            return self._post(action)
        except ApiError as e:
            if e.status == 401:
                self.login_with_remember()
                return self._post(action)
            raise



def digits_of(text):
    """از متن کادر، رقم‌ها را برمی‌گرداند؛ پیشوند G/B (اگر بود) نادیده گرفته می‌شود. در غیر این صورت ''."""
    t = normalize_code(text)
    m = re.fullmatch(r"[GB]?(\d{1,7})", t)
    return m.group(1) if m else ""


KIND_PREFIX = {"tape": "G", "sleeve": "B"}
KIND_LABEL = {"tape": "تپه", "sleeve": "آستین"}
TOL_POS, TOL_W, TOL_CARET = 8, 14, 14
TOL_CLICK_X, TOL_CLICK_Y = 110, 16


def rel_rect(info):
    r, w = info["rect"], info["win"]
    return (r[0] - w[0], r[1] - w[1], r[2] - r[0], r[3] - r[1])


def make_signature(info):
    sig = {"proc": info.get("proc", ""), "mode": info.get("mode", ""),
           "hwnd": info.get("hwnd", 0), "cls": info.get("cls", ""),
           "aid": info.get("aid", "") if usable_aid(info.get("aid")) else ""}
    if info.get("rect") and info.get("win"):
        sig["rel"] = list(rel_rect(info))
    return sig


def _geom(sig, info, column=False, y_max=None):
    """True/False اگر بشود با هندسه تصمیم گرفت، وگرنه None."""
    if not sig or not sig.get("rel") or not info.get("rect") or not info.get("win"):
        return None
    if sig.get("mode") != info.get("mode"):
        return None
    x, y, w, h = sig["rel"]
    ix, iy, iw, ih = rel_rect(info)
    if info["mode"] == "uia":
        if abs(ix - x) > TOL_POS or abs(iw - w) > TOL_W:
            return False
        tol = TOL_POS
    elif info["mode"] == "click":  # نقطه‌ی آخرین کلیک: x تقریبی، y با تلرانس
        if abs(ix - x) > TOL_CLICK_X:
            return False
        tol = TOL_CLICK_Y
    else:  # caret: x با هر حرف تایپ‌شده تغییر می‌کند، فقط y را مقایسه می‌کنیم
        tol = TOL_CARET
    if column:
        if iy < y - tol:
            return False
        return y_max is None or iy < y_max - tol
    return abs(iy - y) <= tol


def plausible_field_rect(rect, win):
    """مستطیل یک کادر متنی واقعی است؟ (نه کل پنجره/تب/صفحه)"""
    if not rect or not win:
        return False
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    ww = max(1, win[2] - win[0])
    return 0 < w <= 0.6 * ww and 0 < h <= 150


def usable_aid(aid):
    """AutomationIdهای عددی (مثل 1901268) دستگیره‌ی پنجره‌اند و هر بار عوض می‌شوند؛ بی‌ارزش."""
    return bool(aid) and not str(aid).isdigit()


def aid_prefix(aid):
    """AutomationId بدون رقم‌های انتهایی: txtSleeveCode3 → txtSleeveCode (ردیف‌های یک ستون یک پیشوند دارند)."""
    return re.sub(r"[\d_]+$", "", aid or "")


def classify(info, calib, auto_names=False):
    """کادر فعال را تشخیص می‌دهد: 'tape' | 'sleeve' | None."""
    calib = calib or {}
    tape, sleeve, bound = calib.get("tape"), calib.get("sleeve"), calib.get("bound")
    aid = info.get("aid") or ""
    if not usable_aid(aid):
        aid = ""

    # --- ۱) AutomationId (برنامه‌های WinForms/WPF): مستقل از جای کادر، اسکرول و اندازه‌ی پنجره
    if aid:
        if tape and usable_aid(tape.get("aid")) and tape.get("aid") == aid:
            return "tape"
        pre = aid_prefix(aid)
        sp = aid_prefix((sleeve or {}).get("aid") if usable_aid((sleeve or {}).get("aid")) else "")
        tp = aid_prefix((tape or {}).get("aid") if usable_aid((tape or {}).get("aid")) else "")
        bp = aid_prefix((bound or {}).get("aid") if usable_aid((bound or {}).get("aid")) else "")
        if len(sp) >= 3 and pre == sp and sp not in (tp, bp):
            return "sleeve"
        if len(bp) >= 3 and pre == bp and bp not in (tp, sp):
            return None  # ستون شلوار: عمداً بی‌اثر

    # --- ۲) هندسه‌ی نسبی به گوشه‌ی پنجره
    if _geom(tape, info) is True:
        return "tape"

    y_max = None
    if bound and bound.get("rel") and bound.get("mode") == (sleeve or {}).get("mode"):
        y_max = bound["rel"][1]
    if _geom(sleeve, info, column=True, y_max=y_max) is True:
        return "sleeve"

    # --- ۳) دستگیره‌ی پنجره‌ی کادر (فقط وقتی برای دو کادر متفاوت ثبت شده)
    if not info.get("rect"):
        th = (tape or {}).get("hwnd")
        sh = (sleeve or {}).get("hwnd")
        if th and sh and th != sh and info.get("hwnd"):
            if info["hwnd"] == th:
                return "tape"
            if info["hwnd"] == sh:
                return "sleeve"

    # --- ۴) آزمایشی: نام گروه‌های بالادست
    if auto_names:
        for n in info.get("names", []):
            if "آستین" in n or "آستين" in n:
                return "sleeve"
            if "تپه" in n or "تپّه" in n:
                return "tape"
    return None


# ----------------------------------------------------------------------------
# ایندکس تپه‌ها و جستجو
# ----------------------------------------------------------------------------
def normalize_code(text):
    return (text or "").translate(PERSIAN_DIGITS).strip().upper()


def canon(text):
    """شکل استاندارد کد: حروف بزرگ، بدون فاصله/خط‌تیره، بدون صفرهای ابتدای عدد (G-012 ≡ g12 ≡ G12)."""
    t = re.sub(r"[^0-9A-Z]", "", normalize_code(text))
    m = re.fullmatch(r"([A-Z]*)0*(\d+)", t)
    return (m.group(1) + m.group(2)) if m else t


def build_index(rows):
    idx = {}
    for r in rows:
        code = canon(r.get("tape", ""))
        if code:
            idx[code] = {
                "box": r.get("box", "") or "",
                "status": r.get("status", "") or "",
                "holder": r.get("holderUsername", "") or "",
            }
    return idx


class TapeIndex:
    """نگه‌داری لیست دو برنامه و جستجوی کد در هر دو."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {key: {} for key, _, _ in SOURCES}
        self.errors = {}
        self.loaded_at = 0

    def refresh(self, api):
        errors = {}
        for key, label, action in SOURCES:
            try:
                js = api.call_authed(action)
                idx = build_index(js.get("rows", []))
                with self.lock:
                    self.data[key] = idx
            except ApiError as e:
                errors[key] = str(e)
        with self.lock:
            self.errors = errors
            self.loaded_at = time.time()
        return errors

    def counts(self):
        with self.lock:
            return {k: len(v) for k, v in self.data.items()}

    def search(self, code):
        code = canon(code)
        found = []
        with self.lock:
            for key, label, _ in SOURCES:
                info = self.data[key].get(code)
                if info:
                    found.append((key, label, info))
        return found


def describe(code, found, errors, label=""):
    """متن و رنگ نتیجه را برمی‌گرداند: (عنوان، [خط‌ها], رنگ)."""
    pre = (label + " ") if label else ""
    if found:
        names = " و ".join(lb for _, lb, _ in found)
        lines = []
        for _, label_, info in found:
            parts = []
            if info["box"]:
                parts.append("باکس %s" % info["box"])
            if info["status"]:
                parts.append(info["status"])
            if info["holder"] and "استفاده" in info["status"]:
                parts.append("(%s)" % info["holder"])
            lines.append("%s: %s" % (label_, " • ".join(parts) if parts else "ثبت شده"))
        return "%s%s  —  موجود در برنامه %s" % (pre, code, names), lines, "#1b7f3b"
    notes = []
    for key, label, _ in SOURCES:
        if key in errors:
            notes.append("%s: خطا در دریافت لیست" % label)
    if notes:
        return "%s%s  —  یافت نشد" % (pre, code), notes, "#b26a00"
    return "%s%s  —  در هیچ‌کدام از دو برنامه نیست" % (pre, code), [], "#a12622"


def describe_both(digits, index, errors, note=""):
    """وقتی نوع کادر (تپه/آستین) معلوم نیست: هر دو را جستجو کن و هر دو را نشان بده."""
    lines, any_found = [], False
    for kind in ("tape", "sleeve"):
        full = KIND_PREFIX[kind] + digits
        found = index.search(full)
        if found:
            any_found = True
            names = " و ".join(lb for _, lb, _ in found)
            det = []
            for _, _lb, info in found:
                bits = []
                if info["box"]:
                    bits.append("باکس %s" % info["box"])
                if info["status"]:
                    bits.append(info["status"])
                det.append(" • ".join(bits) if bits else "ثبت شده")
            lines.append("%s %s: موجود در %s — %s" % (KIND_LABEL[kind], full, names, " | ".join(det)))
        else:
            lines.append("%s %s: نیست" % (KIND_LABEL[kind], full))
    for key, label, _ in SOURCES:
        if key in errors:
            lines.append("⚠ %s: خطا در دریافت لیست" % label)
    if note:
        lines.append(note)
    title = "کد %s  —  %s" % (digits, "موجود است" if any_found else "در هیچ‌کدام نیست")
    return title, lines, ("#1b7f3b" if any_found else "#a12622")


def debug_log(msg):
    """لاگ ساده برای عیب‌یابی (%APPDATA%\\TapeFinder\\debug.log)؛ هیچ‌وقت خطا نمی‌دهد."""
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        path = os.path.join(APP_DIR, "debug.log")
        if os.path.exists(path) and os.path.getsize(path) > 300000:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def why_unknown(info, calib):
    """دلیل کوتاهِ شناسایی‌نشدن کادر (برای نمایش به کاربر)."""
    calib = calib or {}
    if not calib.get("tape") and not calib.get("sleeve"):
        return "هنوز کادری معرفی نشده (Ctrl+Shift+F1 / F2)"
    cur = info.get("mode") or "hwnd"
    cal = "/".join(sorted({(calib[k].get("mode") or "hwnd") for k in ("tape", "sleeve") if calib.get(k)}))
    if cur not in cal.split("/"):
        return "روش خواندن کادر فرق کرد (الان %s، معرفی با %s) — دوباره معرفی کنید" % (cur, cal)
    return "جای کادر با معرفی‌ها نمی‌خواند (روش %s) — دوباره معرفی کنید" % cur


# ----------------------------------------------------------------------------
# بافر کلیدها (منطق خالص، مستقل از ویندوز — قابل تست)
# ----------------------------------------------------------------------------
VK_BACK, VK_TAB, VK_RETURN, VK_ESCAPE = 0x08, 0x09, 0x0D, 0x1B
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_CAPITAL, VK_LSHIFT, VK_RSHIFT = 0x14, 0xA0, 0xA1
VK_LCTRL, VK_RCTRL, VK_LMENU, VK_RMENU = 0xA2, 0xA3, 0xA4, 0xA5
VK_LWIN, VK_RWIN = 0x5B, 0x5C
MODIFIERS = {VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL, VK_LSHIFT, VK_RSHIFT,
             VK_LCTRL, VK_RCTRL, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN}


class KeyBuffer:
    """فقط رقم‌هایی را که پشت‌سرهم تایپ می‌شوند نگه می‌دارد (در برنامه‌ی سفارش بدون G/B تایپ می‌شود)."""
    MAXLEN = 7

    def __init__(self):
        self.buf = ""

    def reset(self):
        self.buf = ""

    def feed(self, vk, ctrl=False, alt=False):
        """
        خروجی:
          'change'  بافر تغییر کرد (جستجوی با تأخیر)
          'commit'  Enter/Tab زده شد (جستجوی فوری، سپس ریست)
          'paste'   Ctrl+V زده شد
          None      بی‌اثر
        """
        if vk in MODIFIERS:
            return None
        if ctrl and vk == 0x56:
            return "paste"
        if ctrl or alt:
            self.reset()
            return None
        if vk in (VK_RETURN, VK_TAB):
            return "commit" if self.buf else None
        if vk == VK_BACK:
            self.buf = self.buf[:-1]
            return "change"
        if 0x30 <= vk <= 0x39:
            ch = chr(vk)
        elif 0x60 <= vk <= 0x69:
            ch = chr(vk - 0x60 + 0x30)
        else:
            self.reset()  # حرف، جهت‌نما، Esc، ... = پایان تایپ کد
            return None
        if len(self.buf) < self.MAXLEN:
            self.buf += ch
        return "change"

    def set_text(self, text):
        self.buf = digits_of(text)[: self.MAXLEN]

    def match(self):
        return self.buf if self.buf else None


# ----------------------------------------------------------------------------
# بخش ویندوز: هوک سراسری کیبورد/ماوس، خواندن پنجره‌ی فعال، کلیپ‌بورد
# ----------------------------------------------------------------------------
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]

    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                           ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = LRESULT
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]

    WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
    WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
    WM_LBUTTONDOWN, WM_RBUTTONDOWN = 0x0201, 0x0204
    WM_GETTEXT, WM_GETTEXTLENGTH = 0x000D, 0x000E
    SMTO_ABORTIFHUNG = 0x0002
    CF_UNICODETEXT = 13

    def foreground_process():
        """(نام exe پنجره‌ی فعال به حروف کوچک، pid)"""
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", 0
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not h:
            return "", pid.value
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower(), pid.value
        finally:
            kernel32.CloseHandle(h)
        return "", pid.value

    def read_focused_text():
        """متن کادر دارای فوکوس (فقط برای کنترل‌های استاندارد ویندوز جواب می‌دهد)."""
        try:
            hwnd = user32.GetForegroundWindow()
            tid = user32.GetWindowThreadProcessId(hwnd, None)
            gti = GUITHREADINFO()
            gti.cbSize = ctypes.sizeof(GUITHREADINFO)
            if not user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) or not gti.hwndFocus:
                return ""
            res = ctypes.c_size_t(0)
            ok = user32.SendMessageTimeoutW(gti.hwndFocus, WM_GETTEXTLENGTH, 0, 0,
                                            SMTO_ABORTIFHUNG, 60, ctypes.byref(res))
            if not ok or res.value == 0 or res.value > 64:
                return ""
            n = res.value + 1
            buf = ctypes.create_unicode_buffer(n)
            ok = user32.SendMessageTimeoutW(gti.hwndFocus, WM_GETTEXT, n,
                                            ctypes.cast(buf, ctypes.c_void_p).value or 0,
                                            SMTO_ABORTIFHUNG, 60, ctypes.byref(res))
            return buf.value if ok else ""
        except Exception:
            return ""

    def read_clipboard():
        try:
            if not user32.OpenClipboard(None):
                return ""
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                p = kernel32.GlobalLock(h)
                if not p:
                    return ""
                try:
                    return ctypes.wstring_at(p)[:64]
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        except Exception:
            return ""


    try:  # با UI Automation دقیق‌ترین نتیجه گرفته می‌شود:  pip install uiautomation
        import uiautomation as auto
        HAVE_UIA = True
    except Exception:
        auto = None
        HAVE_UIA = False

    try:  # مختصات UIA و Win32 در یک مقیاس باشند
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]

    def _uia_text(c):
        for getter in ("GetValuePattern", "GetLegacyIAccessiblePattern"):
            try:
                p = getattr(c, getter)()
                v = getattr(p, "Value", "")
                if v:
                    return str(v)[:64]
            except Exception:
                pass
        return ""

    def probe_field(click=None):
        """هرچه از کادر دارای فوکوس در برنامه‌ی دیگر قابل خواندن است (ترجیحاً UIA، سپس Win32/Caret)."""
        info = {"proc": "", "win": None, "rect": None, "mode": "", "hwnd": 0, "cls": "",
                "ctrl_id": 0, "aid": "", "names": [], "chain": [], "text": "", "uia": HAVE_UIA}
        info["proc"], _ = foreground_process()
        fg = user32.GetForegroundWindow()
        if not fg:
            return info
        wr = wintypes.RECT()
        if user32.GetWindowRect(fg, ctypes.byref(wr)):
            info["win"] = (wr.left, wr.top, wr.right, wr.bottom)

        # --- Win32
        try:
            tid = user32.GetWindowThreadProcessId(fg, None)
            gti = GUITHREADINFO()
            gti.cbSize = ctypes.sizeof(GUITHREADINFO)
            if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                hf = gti.hwndFocus or 0
                info["hwnd"] = hf
                if hf:
                    cb = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hf, cb, 256)
                    info["cls"] = cb.value
                    info["ctrl_id"] = user32.GetDlgCtrlID(hf)
                if gti.hwndCaret and (gti.rcCaret.bottom - gti.rcCaret.top) > 0:
                    pt = wintypes.POINT(gti.rcCaret.left, gti.rcCaret.top)
                    user32.ClientToScreen(gti.hwndCaret, ctypes.byref(pt))
                    h = gti.rcCaret.bottom - gti.rcCaret.top
                    info["caret"] = (pt.x, pt.y, pt.x, pt.y + h)
            info["text"] = read_focused_text()
        except Exception as e:
            info["win32_error"] = str(e)

        # --- UI Automation
        if HAVE_UIA:
            def describe_ctl(c):
                d = {}
                try:
                    br = c.BoundingRectangle
                    d["rect"] = [br.left, br.top, br.right, br.bottom]
                except Exception:
                    d["rect"] = None
                for key, attr in (("aid", "AutomationId"), ("cls", "ClassName"),
                                  ("name", "Name"), ("type", "ControlTypeName")):
                    try:
                        d[key] = getattr(c, attr) or ""
                    except Exception:
                        d[key] = ""
                d["text"] = _uia_text(c)
                chain, names = [], []
                try:
                    p, n = c.GetParentControl(), 0
                    while p is not None and n < 8:
                        chain.append({"type": p.ControlTypeName, "class": p.ClassName,
                                      "name": p.Name, "aid": p.AutomationId})
                        if p.Name and p.ControlTypeName != "WindowControl":
                            names.append(p.Name)
                        p, n = p.GetParentControl(), n + 1
                except Exception:
                    pass
                d["chain"], d["names"] = chain, names
                return d

            cands = {}
            focused = None
            try:
                focused = auto.GetFocusedControl()
                if focused is not None:
                    cands["focused"] = describe_ctl(focused)
            except Exception as e:
                info["uia_error"] = str(e)

            def good(d):
                return bool(d and d.get("rect")) and plausible_field_rect(tuple(d["rect"]), info["win"])

            # اگر UIA فقط یک قاب بزرگ برگرداند (مثل MDI): داخل آن دنبال کادر دارای فوکوس بگرد
            if not good(cands.get("focused")):
                try:
                    deadline = time.time() + 1.5
                    roots = []
                    if info["hwnd"]:
                        roots.append(auto.ControlFromHandle(info["hwnd"]))
                    if focused is not None:
                        roots.append(focused)
                    best = None
                    for r in roots:
                        if r is None or time.time() > deadline:
                            continue
                        for c, depth in auto.WalkControl(r, includeTop=False, maxDepth=14):
                            if time.time() > deadline:
                                break
                            try:
                                if c.HasKeyboardFocus:
                                    d = describe_ctl(c)
                                    if good(d):
                                        best = d
                            except Exception:
                                pass
                        if best:
                            break
                    if best:
                        cands["walk"] = best
                except Exception as e:
                    info["uia_walk_error"] = str(e)

            # آخرین راه: عنصر زیر محل آخرین کلیک
            if not good(cands.get("focused")) and not good(cands.get("walk")) and click:
                try:
                    c = auto.ControlFromPoint(click[0], click[1])
                    if c is not None:
                        cands["point"] = describe_ctl(c)
                except Exception as e:
                    info["uia_point_error"] = str(e)

            info["uia_candidates"] = {k: {kk: vv for kk, vv in v.items() if kk != "chain"}
                                      for k, v in cands.items()}
            pick = None
            for k in ("focused", "walk", "point"):
                if good(cands.get(k)):
                    pick = k
                    break
            chosen = cands.get(pick) if pick else cands.get("focused")
            if chosen:
                info["uia_via"] = pick or "focused(unusable)"
                if chosen.get("rect"):
                    info["rect"], info["mode"] = tuple(chosen["rect"]), "uia"
                info["aid"] = chosen["aid"]
                info["cls"] = info["cls"] or chosen["cls"]
                info["uia_name"], info["uia_type"] = chosen["name"], chosen["type"]
                info["chain"], info["names"] = chosen["chain"], chosen["names"]
                if chosen["text"]:
                    info["text"] = chosen["text"]

        # مستطیل UIA که کل پنجره/تب است (مثل برنامه‌های Chromium) به‌درد نمی‌خورد
        if info["rect"] and not plausible_field_rect(info["rect"], info["win"]):
            info["uia_rect_rejected"] = list(info["rect"])
            info["rect"], info["mode"] = None, ""
            info["aid_ignored"] = info.get("aid", "")
            info["aid"] = ""
        if not info["rect"] and info.get("caret"):
            info["rect"], info["mode"] = info["caret"], "caret"
        if not info["rect"] and click:
            info["click"] = list(click)
            info["rect"] = (click[0] - 1, click[1] - 1, click[0] + 1, click[1] + 1)
            info["mode"] = "click"
        if str(info.get("cls", "")).startswith("Chrome_"):
            info["chromium"] = True
        return info

    class HookThread(threading.Thread):
        """هوک‌های سراسری را در یک ترد جدا (با message loop) اجرا می‌کند و فقط رویدادها را در صف می‌گذارد."""

        def __init__(self, events):
            super().__init__(daemon=True)
            self.events = events
            self._kb = self._ms = None
            self._tid = None

        def run(self):
            events = self.events

            def kb_proc(nCode, wParam, lParam):
                if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    k = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    ctrl = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                    alt = bool(user32.GetAsyncKeyState(VK_MENU) & 0x8000)
                    shift = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                    events.put(("key", k.vkCode, ctrl, alt, shift))
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            def ms_proc(nCode, wParam, lParam):
                if nCode >= 0 and wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN):
                    m = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    events.put(("click", m.pt.x, m.pt.y))
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            self._kb_cb = HOOKPROC(kb_proc)
            self._ms_cb = HOOKPROC(ms_proc)
            hmod = kernel32.GetModuleHandleW(None)
            self._kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_cb, hmod, 0)
            self._ms = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_cb, hmod, 0)
            self._tid = kernel32.GetCurrentThreadId()
            if not self._kb:
                events.put(("error", "نصب هوک کیبورد ناموفق بود (کد %d)" % ctypes.get_last_error()))
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            for h in (self._kb, self._ms):
                if h:
                    user32.UnhookWindowsHookEx(h)

        def stop(self):
            if self._tid:
                user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT

    def make_noactivate(tk_window):
        """پنجره‌ی شناور فوکوس را از برنامه‌ی سفارش نگیرد و کلیک‌ها را هم رد کند."""
        hwnd = user32.GetParent(tk_window.winfo_id()) or tk_window.winfo_id()
        GWL_EXSTYLE = -20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= 0x08000000 | 0x00000080 | 0x00000008  # NOACTIVATE | TOOLWINDOW | TOPMOST
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


else:
    HAVE_UIA = False

    def probe_field(click=None):
        return {}

    def foreground_process():
        return "", 0

    def read_focused_text():
        return ""

    def read_clipboard():
        return ""


# ----------------------------------------------------------------------------
# رابط کاربری
# ----------------------------------------------------------------------------
HOTKEYS = {0x70: "tape", 0x71: "sleeve", 0x72: "bound", 0x73: "diag",
           0x76: "force_tape", 0x77: "force_sleeve"}  # Ctrl+Shift+F1..F4, F7, F8
DIAG_PATH = os.path.join(APP_DIR, "diagnose.txt")
LOG_PATH = os.path.join(APP_DIR, "debug.log")


def run_gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    cfg = load_config()
    api = Api(cfg)
    index = TapeIndex()
    events = queue.Queue()
    keybuf = KeyBuffer()
    st = {"pending": False, "pending_at": 0.0, "kind": None, "kind_known": False,
          "probes": 0, "click": None, "last": "", "hide_at": 0.0, "hook": None, "info": None}

    root = tk.Tk()
    root.title("تپه‌یاب")
    root.geometry("480x640")
    font = ("Tahoma", 10)
    big = ("Tahoma", 12, "bold")

    # ---- پنجره‌ی شناور نتیجه ----
    ov = tk.Toplevel(root)
    ov.withdraw()
    ov.overrideredirect(True)
    ov.attributes("-topmost", True)
    ov_title = tk.Label(ov, font=("Tahoma", 13, "bold"), fg="white", padx=16, pady=6, anchor="e")
    ov_title.pack(fill="x")
    ov_body = tk.Label(ov, font=("Tahoma", 10), fg="white", padx=16, anchor="e", justify="right")
    ov_body.pack(fill="x", pady=(0, 6))
    ov_made = {"v": False}

    def show_overlay(title, lines, color, seconds=None):
        ov.configure(bg=color)
        ov_title.configure(text=title, bg=color)
        ov_body.configure(text="\n".join(lines), bg=color)
        ov.update_idletasks()
        w, h = max(ov.winfo_reqwidth(), 340), ov.winfo_reqheight()
        sw, sh = ov.winfo_screenwidth(), ov.winfo_screenheight()
        ov.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, sh - h - 90))
        ov.deiconify()
        if IS_WIN and not ov_made["v"]:
            ov.update_idletasks()
            make_noactivate(ov)
            ov_made["v"] = True
        ov.lift()
        st["hide_at"] = time.time() + float(seconds or cfg.get("overlay_seconds", 7))

    # ---- فرم ----
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(0, weight=1)
    url_var = tk.StringVar(value=cfg["base_url"])
    user_var = tk.StringVar(value=cfg["username"])
    pass_var = tk.StringVar()
    target_var = tk.StringVar(value=cfg["target_process"])
    enabled_var = tk.BooleanVar(value=cfg["enabled"])
    auto_var = tk.BooleanVar(value=cfg.get("auto_names", False))
    both_var = tk.BooleanVar(value=cfg.get("fallback_both", True))

    def row(label, factory, r):
        ttk.Label(frm, text=label, font=font).grid(row=r, column=1, sticky="e", padx=4, pady=3)
        w = factory()
        w.grid(row=r, column=0, sticky="we", pady=3)

    row("آدرس API", lambda: ttk.Entry(frm, textvariable=url_var), 0)
    row("نام کاربری", lambda: ttk.Entry(frm, textvariable=user_var), 1)
    row("رمز عبور", lambda: ttk.Entry(frm, textvariable=pass_var, show="•"), 2)

    status = tk.StringVar(value="وارد نشده‌اید.")
    ttk.Label(frm, textvariable=status, font=font, foreground="#444", wraplength=440,
              justify="right").grid(row=4, column=0, columnspan=2, sticky="e", pady=4)

    def do_refresh(first_login=None):
        def work():
            try:
                cfg["base_url"] = url_var.get().strip().rstrip("?")
                if first_login:
                    api.login(*first_login)
                errs = index.refresh(api)
                c = index.counts()
                msg = "ستاره جنوب: %d تپه • مروارید: %d تپه" % (c["star"], c["morvarid"])
                for key, label, _ in SOURCES:
                    if key in errs:
                        msg += "\n⚠ %s: %s" % (label, errs[key])
                root.after(0, status.set, msg)
            except ApiError as e:
                root.after(0, status.set, "خطا: %s" % e)
        threading.Thread(target=work, daemon=True).start()

    def on_login():
        if not user_var.get().strip() or not pass_var.get():
            messagebox.showwarning("تپه‌یاب", "نام کاربری و رمز را وارد کنید.")
            return
        status.set("در حال ورود…")
        do_refresh((user_var.get().strip(), pass_var.get()))
        pass_var.set("")

    bf = ttk.Frame(frm)
    bf.grid(row=3, column=0, columnspan=2, sticky="e", pady=4)
    ttk.Button(bf, text="به‌روزرسانی لیست", command=lambda: do_refresh()).pack(side="right", padx=4)
    ttk.Button(bf, text="ورود و دریافت لیست", command=on_login).pack(side="right", padx=4)

    ttk.Separator(frm).grid(row=5, column=0, columnspan=2, sticky="we", pady=6)

    # ---- معرفی کادرها ----
    ttk.Label(frm, text="معرفی کادرهای برنامه‌ی ثبت سفارش", font=("Tahoma", 10, "bold"))\
        .grid(row=6, column=0, columnspan=2, sticky="e")
    help_txt = ("داخل برنامه‌ی ثبت سفارش، نشانگر را در کادر مورد نظر بگذارید و بزنید:\n"
                "Ctrl+Shift+F1 ← کادر «کد» بخش تپه (G)\n"
                "Ctrl+Shift+F2 ← اولین کادر زیر «کد» بخش آستین (B)\n"
                "Ctrl+Shift+F3 ← (اختیاری) اولین کادر «کد» بخش شلوار؛ مرز پایین آستین\n"
                "Ctrl+Shift+F4 ← گزارش تشخیص (برای عیب‌یابی)\n"
                "Ctrl+Shift+F7 / F8 ← جستجوی دستی: عددِ تایپ‌شده را به‌عنوان تپه / آستین جستجو کن")
    ttk.Label(frm, text=help_txt, font=font, justify="right", foreground="#333")\
        .grid(row=7, column=0, columnspan=2, sticky="e", pady=2)
    calib_var = tk.StringVar()
    ttk.Label(frm, textvariable=calib_var, font=font, justify="right", foreground="#0a5")\
        .grid(row=8, column=0, columnspan=2, sticky="e")
    last_var = tk.StringVar(value="")
    ttk.Label(frm, textvariable=last_var, font=font, justify="right", foreground="#666")\
        .grid(row=9, column=0, columnspan=2, sticky="e")

    def refresh_calib_label():
        c = cfg.get("calib", {})
        mark = lambda k: "✔" if c.get(k) else "✘"
        calib_var.set("تپه %s   آستین %s   مرز %s" % (mark("tape"), mark("sleeve"), mark("bound")))
    refresh_calib_label()

    def clear_calib():
        cfg["calib"] = {}
        save_config(cfg)
        refresh_calib_label()

    def open_diag():
        if os.path.exists(DIAG_PATH) and IS_WIN:
            os.startfile(DIAG_PATH)
        else:
            messagebox.showinfo("تپه‌یاب", "هنوز گزارشی ثبت نشده (Ctrl+Shift+F4).")

    cf = ttk.Frame(frm)
    cf.grid(row=10, column=0, columnspan=2, sticky="e", pady=4)
    ttk.Button(cf, text="پاک کردن معرفی‌ها", command=clear_calib).pack(side="right", padx=4)
    ttk.Button(cf, text="باز کردن گزارش تشخیص", command=open_diag).pack(side="right", padx=4)

    def open_log():
        if os.path.exists(LOG_PATH) and IS_WIN:
            os.startfile(LOG_PATH)
        else:
            messagebox.showinfo("تپه‌یاب", "هنوز لاگی ثبت نشده.")
    ttk.Button(cf, text="باز کردن لاگ", command=open_log).pack(side="right", padx=4)

    row("نام exe برنامه‌ی سفارش", lambda: ttk.Entry(frm, textvariable=target_var), 11)
    ttk.Checkbutton(frm, text="جستجوی خودکار فعال باشد", variable=enabled_var)\
        .grid(row=12, column=0, columnspan=2, sticky="e")
    cbf = ttk.Frame(frm)
    cbf.grid(row=13, column=0, columnspan=2, sticky="e")
    ttk.Checkbutton(cbf, text="(آزمایشی) تشخیص کادر از روی نام گروه‌های فرم", variable=auto_var).pack(anchor="e")
    ttk.Checkbutton(cbf, text="اگر کادر شناسایی نشد، هم تپه و هم آستین را جستجو کن (پیشنهادی)",
                    variable=both_var).pack(anchor="e")
    if not HAVE_UIA and IS_WIN:
        ttk.Label(frm, text="⚠ بسته‌ی uiautomation نصب نیست؛ برای دقت بیشتر: pip install uiautomation",
                  foreground="#b26a00", font=font, wraplength=440, justify="right")\
            .grid(row=14, column=0, columnspan=2, sticky="e", pady=3)

    ttk.Separator(frm).grid(row=15, column=0, columnspan=2, sticky="we", pady=6)

    # ---- تست دستی ----
    ttk.Label(frm, text="تست دستی (بدون برنامه‌ی دیگر)", font=font)\
        .grid(row=16, column=0, columnspan=2, sticky="e")
    test_var = tk.StringVar()
    kind_var = tk.StringVar(value="تپه (G)")
    tfrm = ttk.Frame(frm)
    tfrm.grid(row=17, column=0, columnspan=2, sticky="we", pady=3)
    tfrm.columnconfigure(1, weight=1)
    ttk.Combobox(tfrm, textvariable=kind_var, values=["تپه (G)", "آستین (B)"], width=10,
                 state="readonly").grid(row=0, column=2, padx=4)
    te = ttk.Entry(tfrm, textvariable=test_var, font=big, justify="center")
    te.grid(row=0, column=1, sticky="we")

    def show_found(code_digits, kind):
        full = KIND_PREFIX[kind] + code_digits
        t, lines, color = describe(full, index.search(full), index.errors, KIND_LABEL[kind])
        show_overlay(t, lines, color)

    def manual_search(*_):
        d = digits_of(test_var.get())
        if not d:
            show_overlay("؟", ["فقط عدد کد را بنویسید."], "#555555", 3)
            return
        show_found(d, "tape" if kind_var.get().startswith("تپه") else "sleeve")

    ttk.Button(tfrm, text="جستجو", command=manual_search).grid(row=0, column=0, padx=4)
    te.bind("<Return>", manual_search)

    def save_all():
        cfg["base_url"] = url_var.get().strip()
        cfg["target_process"] = target_var.get().strip().lower()
        cfg["enabled"] = bool(enabled_var.get())
        cfg["auto_names"] = bool(auto_var.get())
        cfg["fallback_both"] = bool(both_var.get())
        save_config(cfg)

    # ---- معرفی کادر / گزارش ----
    def handle_hotkey(action, fg):
        if action in ("force_tape", "force_sleeve"):
            kind = "tape" if action == "force_tape" else "sleeve"
            d = keybuf.match() or digits_of(read_focused_text())
            debug_log("جستجوی دستی %s عدد=%r" % (kind, d))
            if d:
                show_found(d, kind)
            else:
                show_overlay("عددی پیدا نشد", ["اول عدد کد را تایپ کنید، بعد همین کلید را بزنید."], "#555555", 4)
            return
        info = probe_field(st.get("click"))
        if action == "diag":
            os.makedirs(APP_DIR, exist_ok=True)
            with open(DIAG_PATH, "a", encoding="utf-8") as f:
                f.write("=== %s ===\n%s\n\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                        json.dumps(info, ensure_ascii=False, indent=1, default=str)))
            show_overlay("گزارش تشخیص ذخیره شد",
                         ["روش: %s • کادر: %s" % (info.get("mode") or "نامشخص", info.get("cls") or "؟"),
                          DIAG_PATH], "#34495e", 6)
            return
        if not info.get("rect") and not info.get("hwnd"):
            show_overlay("کادر شناسایی نشد",
                         ["یک بار داخل کادر کلیک کنید و دوباره بزنید.", "اگر نشد: Ctrl+Shift+F4"], "#a12622", 6)
            return
        if not info.get("rect"):
            show_overlay("هشدار: مختصات کادر خوانده نشد",
                         ["فقط با دستگیره‌ی پنجره معرفی می‌شود؛ احتمال خطا هست."], "#b26a00", 6)
        cfg.setdefault("calib", {})[action] = make_signature(info)
        if not target_var.get().strip() and info.get("proc"):
            target_var.set(info["proc"])
        save_all()
        refresh_calib_label()
        names = {"tape": "کادر کد تپه (G)", "sleeve": "ستون کد آستین (B)", "bound": "مرز پایین آستین"}
        show_overlay("✔ %s معرفی شد" % names[action],
                     ["روش تشخیص: %s" % {"uia": "UI Automation", "caret": "مکان نشانگر متن", "click": "محل آخرین کلیک"}.get(info.get("mode"), "دستگیره‌ی پنجره")],
                     "#1b6ca8", 4)

    # ---- حلقه‌ی اصلی ----
    def trigger_search(use_field_text=True):
        kind = st["kind"]
        digits = keybuf.match() or ""
        if use_field_text and IS_WIN:
            d2 = digits_of(read_focused_text())
            if d2:
                digits = d2
        if not digits:
            return
        if kind:
            if (kind, digits) != st["last"]:
                st["last"] = (kind, digits)
                debug_log("جستجو %s عدد=%s" % (kind, digits))
                show_found(digits, kind)
            return
        # کادر شناسایی نشد → اگر فعال باشد و برنامه‌ی هدف مشخص باشد، هر دو را جستجو کن
        if both_var.get() and target_var.get().strip():
            if ("both", digits) != st["last"]:
                st["last"] = ("both", digits)
                note = why_unknown(st.get("info") or {}, cfg.get("calib"))
                debug_log("جستجوی هر دو (کادر نامشخص) عدد=%s | %s" % (digits, note))
                t, lines, color = describe_both(digits, index, index.errors, "ⓘ " + note)
                show_overlay(t, lines, color)

    def end_typing():
        keybuf.reset()
        st["kind"], st["kind_known"], st["last"], st["pending"], st["probes"] = None, False, "", False, 0

    def process_event(ev, now):
        if ev[0] == "error":
            status.set(ev[1])
            debug_log("خطا: %s" % ev[1])
            return
        fg, pid = foreground_process()
        if pid == os.getpid():
            return
        if ev[0] == "click":
            st["click"] = (ev[1], ev[2])
        if ev[0] == "key" and ev[2] and ev[4] and ev[1] in HOTKEYS:
            handle_hotkey(HOTKEYS[ev[1]], fg)
            return
        tgt = target_var.get().strip().lower()
        if (tgt and fg != tgt) or not enabled_var.get():
            keybuf.reset()
            st["kind"], st["kind_known"] = None, False
            return
        if ev[0] == "click":
            end_typing()
            return

        _, vk, ctrl, alt, _shift = ev
        res = keybuf.feed(vk, ctrl, alt)
        if res == "paste":
            keybuf.set_text(read_clipboard())
            res = "change"
        if not keybuf.buf and res != "change":
            st["kind"], st["kind_known"], st["last"], st["probes"] = None, False, "", 0
        if res in ("change", "commit") and keybuf.buf and not st["kind_known"]:
            # فوکوس هنوز روی همان کادر است: کادر را همین‌جا تشخیص بده
            info = probe_field(st.get("click"))
            st["info"] = info
            st["kind"] = classify(info, cfg.get("calib"), auto_var.get())
            st["probes"] += 1
            # اگر شناسایی نشد، تا ۳ بار (با ارقام بعدی) دوباره تلاش کن
            st["kind_known"] = bool(st["kind"]) or st["probes"] >= 3
            last_var.set("آخرین کادر تشخیص‌داده‌شده: %s" % (KIND_LABEL.get(st["kind"], "نامشخص")))
            debug_log("تشخیص: %s | proc=%s mode=%s via=%s cls=%s aid=%s rect=%s win=%s" % (
                st["kind"], info.get("proc"), info.get("mode"), info.get("uia_via"), info.get("cls"),
                info.get("aid"), info.get("rect"), info.get("win")))
        if res == "commit":
            trigger_search(use_field_text=False)
            end_typing()
        elif res == "change":
            st["pending"], st["pending_at"] = True, now + 0.30

    def tick():
        # هر خطای داخلی فقط لاگ می‌شود؛ حلقه هرگز متوقف نمی‌شود
        try:
            now = time.time()
            while True:
                try:
                    ev = events.get_nowait()
                except queue.Empty:
                    break
                try:
                    process_event(ev, now)
                except Exception:
                    import traceback
                    debug_log("استثنا در پردازش رویداد:\n" + traceback.format_exc())
            if st["pending"] and now >= st["pending_at"]:
                st["pending"] = False
                try:
                    trigger_search()
                except Exception:
                    import traceback
                    debug_log("استثنا در جستجو:\n" + traceback.format_exc())
                    try:
                        show_overlay("خطای داخلی", ["جزئیات در لاگ ثبت شد (دکمه‌ی «باز کردن لاگ»)."], "#a12622", 5)
                    except Exception:
                        pass
            if st["hide_at"] and now >= st["hide_at"]:
                ov.withdraw()
                st["hide_at"] = 0
        except Exception:
            import traceback
            debug_log("استثنا در tick:\n" + traceback.format_exc())
        root.after(80, tick)

    def periodic():
        if cfg.get("remember_token"):
            do_refresh()
        root.after(max(1, int(cfg.get("refresh_minutes", 5))) * 60 * 1000, periodic)

    def on_close():
        save_all()
        if st["hook"]:
            st["hook"].stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    if IS_WIN:
        st["hook"] = HookThread(events)
        st["hook"].start()
    else:
        status.set("⚠ هوک کیبورد فقط روی ویندوز کار می‌کند؛ تست دستی در دسترس است.")

    if cfg.get("remember_token"):
        status.set("در حال دریافت لیست‌ها…")
        do_refresh()
    root.after(80, tick)
    root.after(max(1, int(cfg.get("refresh_minutes", 5))) * 60 * 1000, periodic)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
