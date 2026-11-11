"""
BurnFix - per-pixel burn-in compensation overlay for OLED panels.

Burned-in pixels have lost efficiency: they emit a few percent less light than
their neighbours for the same signal. This tool keeps a per-pixel map of that
loss and puts an invisible, click-through layer over the desktop that trims the
healthy pixels around each burn mark by exactly the missing amount, fading out
to nothing over a couple of hundred pixels so the trim itself can't be seen.
Marks that show up brighter than their surroundings are trimmed directly.
Nothing is ever driven harder, so no pixel ages faster because of it.

  python burnfix.py calibrate   interactive editor (paint the burn, null it by eye)
  python burnfix.py run         background overlay (Ctrl+Alt+B toggles it)
  python burnfix.py stop        stop the background overlay
  python burnfix.py install     start the overlay at every logon
  python burnfix.py uninstall   remove the logon shortcut and stop the overlay
  python burnfix.py selftest    verify the blend works on this machine

Windows only. Needs numpy and pillow.
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes as wt

import numpy as np
from PIL import Image, ImageFilter, ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_FILE = os.path.join(HERE, "burnmap.npz")
LOCK_FILE = os.path.join(HERE, "editing.lock")
STOP_FILE = os.path.join(HERE, "stop.flag")
MUTEX_NAME = "BurnFixOverlayRuntime"

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
ULW_ALPHA = 2
WDA_EXCLUDEFROMCAPTURE = 0x11


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                       ctypes.POINTER(wt.SIZE), wt.HDC,
                                       ctypes.POINTER(wt.POINT), wt.COLORREF,
                                       ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
kernel32.OpenMutexW.restype = wt.HANDLE
kernel32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


class Overlay:
    """Full-screen, click-through, always-on-top black layer with per-pixel alpha.
    Black at alpha a turns every pixel beneath into pixel * (1 - a/255)."""

    def __init__(self, w, h, capturable=False):
        self.w, self.h = w, h
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, "Static", "BurnFixOverlay", WS_POPUP,
                                           0, 0, w, h, None, None, None, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.sdc = user32.GetDC(None)
        self.mdc = gdi32.CreateCompatibleDC(self.sdc)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(self.sdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not self.hbmp:
            raise ctypes.WinError(ctypes.get_last_error())
        gdi32.SelectObject(self.mdc, self.hbmp)
        self.buf = np.ctypeslib.as_array(ctypes.cast(bits, ctypes.POINTER(ctypes.c_ubyte)),
                                         shape=(h, w, 4))
        self.buf[:] = 0
        self.visible = False
        if not capturable:
            # keep the correction out of screenshots / recordings / streams
            user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)
        self.push()

    def set_alpha(self, a):
        self.buf[:, :, 3] = a
        self.push()

    def push(self):
        ok = user32.UpdateLayeredWindow(self.hwnd, self.sdc, ctypes.byref(wt.POINT(0, 0)),
                                        ctypes.byref(wt.SIZE(self.w, self.h)), self.mdc,
                                        ctypes.byref(wt.POINT(0, 0)), 0,
                                        ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)), ULW_ALPHA)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def show(self, on=True):
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if on else SW_HIDE)
        self.visible = on
        if on:
            self.raise_top()

    def raise_top(self):
        user32.SetWindowPos(self.hwnd, wt.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def destroy(self):
        user32.DestroyWindow(self.hwnd)


N_LAYERS = 9


class Model:
    """Burn map = sum of layers. Each layer is a painted mask (0..255) times one
    strength, so a mark can be drawn once and then dialled in until it vanishes.
    Strength is in overlay alpha steps (1 step ~ 0.4% signal ~ 0.9% brightness)."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.masks = [None] * N_LAYERS
        self.strength = [0.0] * N_LAYERS
        self.tex = [False] * N_LAYERS
        self.feather = True
        self.stencil = None  # float32 0..1, brightness of the image that caused the burn
        self.noise = np.random.default_rng(1).random((h, w), dtype=np.float32)

    def mask(self, i):
        if self.masks[i] is None:
            self.masks[i] = np.zeros((self.h, self.w), np.uint8)
        return self.masks[i]

    def burn(self):
        b = np.zeros((self.h, self.w), np.float32)
        for i in range(N_LAYERS):
            if self.masks[i] is None or self.strength[i] == 0:
                continue
            m = self.masks[i].astype(np.float32)
            m *= self.strength[i] / 255.0
            if self.tex[i] and self.stencil is not None:
                m *= self.stencil
            b += m
        return b

    def envelope(self, b, d, radius=26, f=8):
        """Smooth surface that is >= b everywhere and falls to 0 about
        radius*f pixels away from any burn mark."""
        h, w = b.shape
        ph, pw = (-h) % f, (-w) % f
        bp = np.pad(b, ((0, ph), (0, pw)), mode="edge") if (ph or pw) else b
        ds = bp.reshape(bp.shape[0] // f, f, bp.shape[1] // f, f).max(axis=(1, 3))
        for axis in (0, 1):  # separable square dilation by radius+2 cells
            for _ in range(radius + 2):
                p = np.pad(ds, [(1, 1) if k == axis else (0, 0) for k in (0, 1)], mode="edge")
                lo = p[:-2] if axis == 0 else p[:, :-2]
                hi = p[2:] if axis == 0 else p[:, 2:]
                ds = np.maximum(ds, np.maximum(lo, hi))
        img = Image.fromarray(np.clip(np.ceil(ds / d * 255.0), 0, 255).astype(np.uint8))
        img = img.filter(ImageFilter.BoxBlur(radius / 2)).filter(ImageFilter.BoxBlur(radius / 2))
        up = img.resize((bp.shape[1], bp.shape[0]), Image.BILINEAR)
        e = np.asarray(up, np.float32)[:h, :w] * np.float32(d / 255.0)
        return np.maximum(e, b)

    def alpha(self):
        b = self.burn()
        if not b.any():
            return np.zeros((self.h, self.w), np.uint8)
        # negative strength = the mark is BRIGHTER than its surroundings: trim the mark itself
        a = np.maximum(-b, 0)
        bp = np.maximum(b, 0)
        d = float(bp.max())
        if d > 0:  # positive = the mark is darker: trim the healthy pixels around it
            a += (self.envelope(bp, d) if self.feather else np.float32(d)) - bp
        a += self.noise  # static dither: gives finer-than-one-step correction
        np.floor(a, out=a)
        return np.clip(a, 0, 255).astype(np.uint8)

    def save(self, path=MAP_FILE):
        data = {"size": np.array([self.w, self.h]), "strength": np.array(self.strength),
                "tex": np.array(self.tex), "feather": np.array(self.feather)}
        for i, m in enumerate(self.masks):
            if m is not None and m.any():
                data[f"mask{i}"] = m
        if self.stencil is not None and any(self.tex):
            data["stencil"] = self.stencil.astype(np.float16)
        tmp = path + ".tmp.npz"
        np.savez_compressed(tmp, **data)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path=MAP_FILE):
        z = np.load(path)
        w, h = (int(v) for v in z["size"])
        m = cls(w, h)
        m.strength = [float(v) for v in z["strength"]]
        m.tex = [bool(v) for v in z["tex"]]
        m.feather = bool(z["feather"])
        for i in range(N_LAYERS):
            if f"mask{i}" in z:
                m.masks[i] = z[f"mask{i}"]
        if "stencil" in z:
            m.stencil = z["stencil"].astype(np.float32)
        return m


def runtime_running():
    h = kernel32.OpenMutexW(0x00100000, False, MUTEX_NAME)
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


def pythonw():
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return exe if os.path.exists(exe) else sys.executable


def start_runtime():
    if runtime_running():
        return
    subprocess.Popen([pythonw(), os.path.abspath(__file__), "run"], cwd=HERE,
                     creationflags=0x00000008 | 0x00000200, close_fds=True)


def startup_link():
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup", "BurnFix.lnk")


def install():
    """Start the overlay at every logon (a shortcut in the user's Startup folder)."""
    q = lambda s: s.replace("'", "''")
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
          "$s.TargetPath='%s';$s.Arguments='\"%s\" run';$s.WorkingDirectory='%s';$s.Save()"
          % (q(startup_link()), q(pythonw()), q(os.path.abspath(__file__)), q(HERE)))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print("installed:", startup_link())
    start_runtime()


def uninstall():
    try:
        os.remove(startup_link())
        print("removed:", startup_link())
    except OSError:
        print("no startup shortcut found")
    stop()


def stop():
    if runtime_running():
        open(STOP_FILE, "w").close()
        print("overlay asked to stop")
    else:
        print("overlay is not running")


# --------------------------------------------------------------------------- run
def run():
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:
        return  # already running
    ov = None
    mtime = None
    enabled = True
    was_down = False
    last_slow = 0.0
    msg = wt.MSG()
    while True:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        # Ctrl+Alt+B toggles the correction
        down = all(user32.GetAsyncKeyState(k) & 0x8000 for k in (0x11, 0x12, 0x42))
        if down and not was_down:
            enabled = not enabled
            last_slow = 0
        was_down = down
        now = time.time()
        if now - last_slow > 1.0:
            last_slow = now
            if os.path.exists(STOP_FILE):
                try:
                    os.remove(STOP_FILE)
                except OSError:
                    pass
                if ov:
                    ov.destroy()
                return
            try:
                mt = os.path.getmtime(MAP_FILE)
            except OSError:
                mt = None
            if mt != mtime:
                mtime = mt
                if ov:
                    ov.destroy()
                    ov = None
                if mt is not None:
                    try:
                        model = Model.load()
                        ov = Overlay(model.w, model.h)
                        ov.set_alpha(model.alpha())
                        del model
                    except Exception:
                        ov = None
            if ov:
                want = (enabled and not os.path.exists(LOCK_FILE)
                        and screen_size() == (ov.w, ov.h))
                if want != ov.visible:
                    ov.show(want)
                elif want:
                    ov.raise_top()
        time.sleep(0.05)


# ---------------------------------------------------------------------- selftest
def selftest():
    import tkinter as tk

    w, h = screen_size()
    box = (w // 2 - 100, h // 2 - 100, w // 2 + 100, h // 2 + 100)
    root = tk.Tk()  # a known grey patch to measure against
    root.overrideredirect(True)
    root.geometry(f"400x400+{w // 2 - 200}+{h // 2 - 200}")
    root.configure(bg="#b4b4b4")
    root.attributes("-topmost", True)
    root.update()
    time.sleep(0.4)
    before = np.asarray(ImageGrab.grab(box).convert("RGB"), np.float32)
    ov = Overlay(w, h, capturable=True)
    a = np.zeros((h, w), np.uint8)
    a[box[1]:box[3], box[0]:box[2]] = 51  # 20% trim for a moment
    ov.set_alpha(a)
    ov.show()
    root.update()
    time.sleep(0.4)
    after = np.asarray(ImageGrab.grab(box).convert("RGB"), np.float32)
    ov.destroy()
    root.destroy()
    ratio = float(after.mean() / before.mean())
    print(f"screen {w}x{h}; measured ratio {ratio:.3f} (expected 0.800)")
    print("PASS" if abs(ratio - 0.8) < 0.02 else "FAIL - the overlay is not blending as expected")
    m = Model(w, h)
    m.mask(0)[500:700, 1000:1400] = 255
    m.strength[0] = 4.0
    t = time.time()
    al = m.alpha()
    print(f"map build {time.time() - t:.2f}s; alpha inside mark {al[600, 1200]}, "
          f"just outside {al[600, 990]}, far away {al[50, 50]}")


# --------------------------------------------------------------------- calibrate
HELP = """BURNFIX CALIBRATION                                   (Tab hides / shows this text)

Look for the burn marks on this flat screen. For each mark:
  1. Pick a layer with 1-9  (use a new layer for each mark that has a different depth)
  2. Paint over the mark    left-drag = paint, right-drag = erase, mouse wheel = brush size
                            R = rectangle tool, B = back to brush
  3. Up / Down arrows       dial the strength until the mark melts into the background
                            (hold Shift for big steps). If Up makes it WORSE, the mark is brighter
                            than its surroundings: keep pressing Down, through zero into negative.

  T      shape this layer from the screenshot taken at launch (logo, taskbar, UI)
         instead of a flat fill - paint roughly over the area, the detail comes from the image
  G / C  change grey level / colour of the test screen - check the fix on several
  Space  compare: correction off / on          F  feathered (default) or whole-screen trim
  Z      undo last stroke                       X  clear this layer
  Esc    save and start the overlay             Ctrl+Q  quit without saving"""


def calibrate():
    import tkinter as tk

    w, h = screen_size()
    open(LOCK_FILE, "w").close()  # tells a running overlay to stand down
    time.sleep(1.3)
    try:
        shot = ImageGrab.grab((0, 0, w, h)).convert("RGB")
        stencil = (np.asarray(shot, np.float32).max(axis=2) / 255.0) ** 2.2
    except Exception:
        stencil = None

    if os.path.exists(MAP_FILE):
        try:
            model = Model.load()
            if (model.w, model.h) != (w, h):
                model = Model(w, h)
        except Exception:
            model = Model(w, h)
    else:
        model = Model(w, h)
    if model.stencil is None or not any(model.tex):
        model.stencil = stencil

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{w}x{h}+0+0")
    cv = tk.Canvas(root, width=w, height=h, highlightthickness=0, cursor="none")
    cv.pack()
    ov = Overlay(w, h)

    st = dict(layer=0, radius=60, tool="brush", grey=1, col=0, show=True, help=True,
              dirty=True, last=None, undo=None, rect0=None, saved=False)
    greys = [64, 128, 190, 255, 24]
    cols = [(1, 1, 1), (1, 0, 0), (0, 1, 0), (0, 0, 1)]

    help_id = cv.create_text(60, 70, anchor="nw", text=HELP, font=("Consolas", 13))
    stat_id = cv.create_text(60, 30, anchor="nw", text="", font=("Consolas", 13, "bold"))
    ring = cv.create_oval(0, 0, 0, 0)
    rect_id = cv.create_rectangle(0, 0, 0, 0, state="hidden")

    def restyle():
        g = greys[st["grey"]]
        c = cols[st["col"]]
        bg = "#%02x%02x%02x" % tuple(int(g * k) for k in c)
        t = g + 70 if g < 150 else g - 70
        fg = "#%02x%02x%02x" % (t, t, t)
        cv.configure(bg=bg)
        for i in (help_id, stat_id):
            cv.itemconfigure(i, fill=fg)
        cv.itemconfigure(ring, outline=fg)
        cv.itemconfigure(rect_id, outline=fg)
        cv.itemconfigure(help_id, state="normal" if st["help"] else "hidden")
        cv.itemconfigure(stat_id, state="normal" if st["help"] else "hidden")

    def status():
        i = st["layer"]
        s = model.strength[i]
        cv.itemconfigure(stat_id, text=(
            f"layer {i + 1}   strength {s:+.2f} ({'lifting a dark mark' if s > 0 else 'dimming a bright mark' if s < 0 else 'off'})   "
            f"{'screenshot-shaped' if model.tex[i] else 'flat'}   tool: {st['tool']}   "
            f"brush {st['radius']}px   {'feathered' if model.feather else 'whole-screen'}   "
            f"correction {'ON' if st['show'] else 'OFF'}"))

    def mark():
        st["dirty"] = True
        status()

    def tick():
        if st["dirty"]:
            st["dirty"] = False
            ov.set_alpha(model.alpha() if st["show"] else 0)
        root.after(40, tick)

    def stamp(x, y, erase):
        r = st["radius"]
        m = model.mask(st["layer"])
        x0, x1, y0, y1 = max(0, x - r), min(w, x + r + 1), max(0, y - r), min(h, y + r + 1)
        if x0 >= x1 or y0 >= y1:
            return
        yy, xx = np.ogrid[y0 - y:y1 - y, x0 - x:x1 - x]
        d = np.sqrt(xx * xx + yy * yy) / r
        fall = (np.clip((1.0 - d) / 0.35, 0, 1) * 255).astype(np.uint8)  # soft outer 35%
        reg = m[y0:y1, x0:x1]
        if erase:
            np.minimum(reg, 255 - fall, out=reg)
        else:
            np.maximum(reg, fall, out=reg)

    def begin(e, erase):
        i = st["layer"]
        st["undo"] = (i, model.mask(i).copy())
        if model.strength[i] == 0 and not erase:
            model.strength[i] = 4.0  # visible starting point; dial it in afterwards
        if st["tool"] == "rect":
            st["rect0"] = (e.x, e.y)
            cv.itemconfigure(rect_id, state="normal")
            cv.coords(rect_id, e.x, e.y, e.x, e.y)
        else:
            st["last"] = (e.x, e.y)
            stamp(e.x, e.y, erase)
            mark()

    def drag(e, erase):
        move(e)
        if st["tool"] == "rect":
            if st["rect0"]:
                cv.coords(rect_id, *st["rect0"], e.x, e.y)
            return
        if st["last"] is None:
            return
        lx, ly = st["last"]
        dist = max(abs(e.x - lx), abs(e.y - ly))
        n = max(1, int(dist / max(2, st["radius"] / 3)))
        for k in range(1, n + 1):
            stamp(int(lx + (e.x - lx) * k / n), int(ly + (e.y - ly) * k / n), erase)
        st["last"] = (e.x, e.y)
        mark()

    def end(e, erase):
        if st["tool"] == "rect" and st["rect0"]:
            x0, y0 = st["rect0"]
            xa, xb = sorted((max(0, min(w, x0)), max(0, min(w, e.x))))
            ya, yb = sorted((max(0, min(h, y0)), max(0, min(h, e.y))))
            model.mask(st["layer"])[ya:yb, xa:xb] = 0 if erase else 255
            st["rect0"] = None
            cv.itemconfigure(rect_id, state="hidden")
            mark()
        st["last"] = None

    def move(e):
        r = st["radius"] if st["tool"] == "brush" else 4
        cv.coords(ring, e.x - r, e.y - r, e.x + r, e.y + r)

    def wheel(e):
        up = e.delta > 0
        st["radius"] = int(min(600, max(3, st["radius"] * (1.15 if up else 1 / 1.15) + (1 if up else -1))))
        move(e)
        status()

    def key(e):
        k = e.keysym
        kl = k.lower()
        i = st["layer"]
        shift, ctrl = bool(e.state & 1), bool(e.state & 4)
        if k in ("Up", "Down"):
            step = (1.0 if shift else 0.25) * (1 if k == "Up" else -1)
            model.strength[i] = min(60.0, max(-60.0, model.strength[i] + step))
        elif len(k) == 1 and k in "123456789":
            st["layer"] = int(k) - 1
        elif kl == "q":
            if ctrl:
                root.destroy()
            return
        elif kl == "b":
            st["tool"] = "brush"
        elif kl == "r":
            st["tool"] = "rect"
        elif kl == "t":
            model.tex[i] = not model.tex[i]
        elif kl == "f":
            model.feather = not model.feather
        elif kl == "g":
            st["grey"] = (st["grey"] + 1) % len(greys)
            restyle()
        elif kl == "c":
            st["col"] = (st["col"] + 1) % len(cols)
            restyle()
        elif k == "space":
            st["show"] = not st["show"]
        elif k == "Tab":
            st["help"] = not st["help"]
            restyle()
            return "break"
        elif kl == "z":
            if not st["undo"]:
                return
            j, old = st["undo"]
            model.masks[j] = old
            st["undo"] = None
        elif kl == "x":
            st["undo"] = (i, model.mask(i).copy())
            model.masks[i][:] = 0
        elif k == "Escape":
            model.save()
            st["saved"] = True
            root.destroy()
            return
        else:
            return
        mark()

    cv.bind("<Motion>", move)
    for btn, erase in ((1, False), (3, True)):
        cv.bind(f"<ButtonPress-{btn}>", lambda e, er=erase: begin(e, er))
        cv.bind(f"<B{btn}-Motion>", lambda e, er=erase: drag(e, er))
        cv.bind(f"<ButtonRelease-{btn}>", lambda e, er=erase: end(e, er))
    root.bind("<MouseWheel>", wheel)
    root.bind("<Key>", key)

    restyle()
    status()
    ov.show()
    root.after(100, lambda: (root.lift(), root.focus_force(), ov.raise_top()))
    root.after(40, tick)
    try:
        root.mainloop()
    finally:
        ov.destroy()
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    if st["saved"]:
        start_runtime()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "run":
        try:
            os.remove(LOCK_FILE)  # stale lock from a crashed editor
        except OSError:
            pass
        run()
    elif cmd == "selftest":
        selftest()
    elif cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "stop":
        stop()
    elif cmd == "calibrate":
        calibrate()
    else:
        print(__doc__)
