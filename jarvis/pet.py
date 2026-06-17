"""桌面贾维斯——钢铁侠 JARVIS 风格的全息控制台面板（电影深青版）。

灵感来自 Rainmeter「JARVIS Display System」皮肤，宽版双栏布局：
  左栏：时钟/日期、中央弧反应堆、状态、音频波形
  右栏：天气、系统遥测(磁盘/电量/CPU/运行时长)、笔记待办、转写台账
深青电影色调 + 扫描线/网格 CRT 背景 + 缓慢扫掠高光线。

特性：
  - 无边框、始终置顶、可鼠标拖动（拖面板任意处）
  - 中央弧反应堆随语音状态变色/动画：
      待机(青) / 聆听(青绿) / 思考(琥珀+旋转) / 说话(亮青脉动)
  - 音频波形随状态起伏（说话/聆听时活跃，待机时平缓）
  - 笔记栏读取项目根 notes.txt（每行一条，自动刷新）；无则读 memory.json
  - 实时系统遥测；底部转写台账滚动显示「你说的话 / 贾维斯回答」
  - 点一下反应堆即可开始说话（无需喊唤醒词）；双击或 Esc 关闭

GUI 跑在主线程，语音助手跑在后台线程，通过线程安全队列通信。
对外接口与旧版一致：set_state / log / heard / reply / poll_talk / run。
"""

from __future__ import annotations

import glob
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


def _fix_tcltk_env() -> None:
    """uv/独立版 Python 自带 tkinter 但常找不到 Tcl/Tk 数据文件，
    导入 tkinter 前把 TCL_LIBRARY/TK_LIBRARY 指到 base_prefix/lib 下。"""
    specs = [("TCL_LIBRARY", "tcl*", "init.tcl"),
             ("TK_LIBRARY", "tk*", "tk.tcl")]
    for var, pattern, marker in specs:
        if os.environ.get(var):
            continue
        for prefix in (sys.base_prefix, sys.prefix):
            for d in sorted(glob.glob(os.path.join(prefix, "lib", pattern)),
                            reverse=True):
                if os.path.exists(os.path.join(d, marker)):
                    os.environ[var] = d
                    break
            if os.environ.get(var):
                break


_fix_tcltk_env()

import tkinter as tk  # noqa: E402

from PIL import Image, ImageDraw, ImageFont, ImageTk  # noqa: E402

# ---- 面板尺寸（逻辑像素）+ 超采样倍率 -------------------------------
W, H = 720, 600
S = 2                           # 内部 2x 渲染再缩小，边缘更顺滑
ROOT = Path(__file__).resolve().parent.parent

# 左右分栏
DIV_X = 314                     # 竖直分隔线
LX = 26                         # 左栏左边界
RX = 336                        # 右栏左边界
RXE = W - 24                    # 右栏右边界
CXL, CYL = 168, 256             # 弧反应堆中心（左栏）
REACTOR_R = 76

# 深青电影色调
TEAL = (40, 188, 205)           # 主色
TEAL_HI = (130, 240, 248)       # 高亮
TEAL_DIM = (24, 92, 104)        # 暗
GRID = (32, 120, 130)           # 网格线
INK = (4, 9, 11)                # 面板底色

STATE_COLOR = {
    "idle": (40, 190, 208),
    "listening": (52, 220, 158),
    "thinking": (236, 182, 92),
    "speaking": (118, 236, 248),
}
STATE_LABEL = {
    "idle": "STANDBY", "listening": "LISTENING",
    "thinking": "PROCESSING", "speaking": "SPEAKING",
}

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_MONO_PATHS = (
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
)
_HAN_PATHS = (
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)


def _font(paths: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    key = (paths[0], size)
    if key not in _FONT_CACHE:
        for p in paths:
            if os.path.exists(p):
                _FONT_CACHE[key] = ImageFont.truetype(p, size)
                break
        else:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _mono(size: int) -> ImageFont.FreeTypeFont:
    return _font(_MONO_PATHS, size * S)


def _han(size: int) -> ImageFont.FreeTypeFont:
    return _font(_HAN_PATHS, size * S)


# ---- 系统遥测 / 天气 / 笔记：全部标准库，带节流缓存 -----------------

class Telemetry:
    def __init__(self) -> None:
        self._boot = self._read_boottime()
        self._ncpu = os.cpu_count() or 8
        self._cache: dict = {"batt": (None, False), "disk": (0, 0)}
        self._next = {"batt": 0.0, "disk": 0.0}

    @staticmethod
    def _read_boottime() -> float:
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", "kern.boottime"], text=True)
            return float(out.split("sec =")[1].split(",")[0].strip())
        except Exception:  # noqa: BLE001
            return time.time()

    def disk(self) -> tuple[float, float]:
        if time.time() >= self._next["disk"]:
            du = shutil.disk_usage("/")
            self._cache["disk"] = (du.total / 1e9, du.free / 1e9)
            self._next["disk"] = time.time() + 10
        return self._cache["disk"]

    def battery(self) -> tuple[int | None, bool]:
        if time.time() >= self._next["batt"]:
            pct, charging = None, False
            try:
                out = subprocess.check_output(["pmset", "-g", "batt"], text=True)
                line = out.strip().splitlines()[-1]
                for tok in line.replace(";", " ").split():
                    if tok.endswith("%"):
                        pct = int(tok[:-1])
                        break
                charging = ("charging" in line) or ("charged" in line)
            except Exception:  # noqa: BLE001
                pass
            self._cache["batt"] = (pct, charging)
            self._next["batt"] = time.time() + 5
        return self._cache["batt"]

    def uptime(self) -> str:
        secs = max(0, int(time.time() - self._boot))
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        return f"{d}D {h:02d}H {m:02d}M" if d else f"{h:02d}H {m:02d}M"

    def load(self) -> tuple[float, float]:
        try:
            la = os.getloadavg()[0]
        except OSError:
            la = 0.0
        return la, min(1.0, la / self._ncpu)


class Weather:
    def __init__(self) -> None:
        self.text: str | None = None
        self.temp: str | None = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                req = urllib.request.Request(
                    "https://wttr.in/?format=%t|%C",
                    headers={"User-Agent": "curl/8"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    raw = r.read().decode("utf-8").strip()
                if "|" in raw and "Unknown" not in raw:
                    t, c = raw.split("|", 1)
                    self.temp = t.replace("+", "").strip()
                    self.text = c.strip().upper()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1200)


class Notes:
    """笔记/待办来源：优先 notes.txt（每行一条），否则读 memory.json 的 facts。
    每 5 秒重读一次，编辑文件后面板自动更新。"""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._next = 0.0

    def items(self) -> list[str]:
        if time.time() >= self._next:
            self._items = self._read()
            self._next = time.time() + 5
        return self._items

    @staticmethod
    def _read() -> list[str]:
        nf = ROOT / "notes.txt"
        if nf.exists():
            out = []
            for ln in nf.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    out.append(ln.lstrip("-•* ").strip())
            if out:
                return out
        mj = ROOT / "memory.json"
        if mj.exists():
            try:
                data = json.loads(mj.read_text(encoding="utf-8"))
                return [it.get("fact", "") for it in data if it.get("fact")]
            except (json.JSONDecodeError, OSError):
                pass
        return ["（暂无笔记 · 编辑 notes.txt 或对我说「记住…」）"]


class DesktopPet:
    def __init__(self) -> None:
        self._q: queue.Queue[tuple] = queue.Queue()
        self._state = "idle"
        self._phase = 0.0
        self._lines: list[tuple[str, str]] = []
        self._tele = Telemetry()
        self._weather = Weather()
        self._notes = Notes()
        self._bg: Image.Image | None = None
        self.talk_event = threading.Event()

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        for attr in ("-transparent",):
            try:
                self.root.wm_attributes(attr, True)
            except tk.TclError:
                pass
        try:
            self.root.config(bg="systemTransparent")
        except tk.TclError:
            self.root.config(bg="black")

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self._x, self._y = sw - W - 32, max(36, (sh - H) // 2)
        self.root.geometry(f"{W}x{H}+{self._x}+{self._y}")

        try:
            self.canvas = tk.Canvas(self.root, width=W, height=H,
                                    bg="systemTransparent",
                                    highlightthickness=0, bd=0)
        except tk.TclError:
            self.canvas = tk.Canvas(self.root, width=W, height=H,
                                    bg="black", highlightthickness=0, bd=0)
        self.canvas.pack()
        self._img_id = self.canvas.create_image(0, 0, anchor="nw")
        self._photo = None

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-1>", lambda e: self.root.destroy())
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        self.root.after(50, self._tick)

    # ---- 线程安全接口（签名与旧版一致）----------------------------
    def set_state(self, state: str) -> None:
        self._q.put(("state", state))

    def log(self, text: str) -> None:
        print(text)

    def heard(self, text: str) -> None:
        self._q.put(("line", ("you", text)))

    def reply(self, text: str) -> None:
        self._q.put(("line", ("jarvis", text)))

    def poll_talk(self) -> bool:
        if self.talk_event.is_set():
            self.talk_event.clear()
            return True
        return False

    def run(self) -> None:
        self.root.mainloop()

    # ---- 鼠标交互 ----------------------------------------------------
    def _press(self, e: tk.Event) -> None:
        self._drag_origin = (e.x_root, e.y_root, self._x, self._y)
        self._moved = False
        self._press_xy = (e.x, e.y)

    def _drag(self, e: tk.Event) -> None:
        ox, oy, wx, wy = self._drag_origin
        dx, dy = e.x_root - ox, e.y_root - oy
        if abs(dx) > 3 or abs(dy) > 3:
            self._moved = True
        self._x, self._y = wx + dx, wy + dy
        self.root.geometry(f"+{self._x}+{self._y}")

    def _release(self, e: tk.Event) -> None:
        if self._moved:
            return
        px, py = self._press_xy
        if (px - CXL) ** 2 + (py - CYL) ** 2 <= (REACTOR_R * 1.4) ** 2:
            self.talk_event.set()
            self._state = "listening"
            self._push_line("sys", "我在听，请说…")

    def _push_line(self, role: str, text: str) -> None:
        self._lines.append((role, text))
        self._lines = self._lines[-6:]

    # ---- 缩放绘图原语（逻辑坐标 → 超采样画布）----------------------
    def _ell(self, d, cx, cy, r, **kw) -> None:
        d.ellipse([(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S], **kw)

    def _arc(self, d, cx, cy, r, a0, a1, width, fill) -> None:
        d.arc([(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S],
              a0, a1, fill=fill, width=max(1, int(width * S)))

    def _line(self, d, x0, y0, x1, y1, width, fill) -> None:
        d.line([x0 * S, y0 * S, x1 * S, y1 * S], fill=fill,
               width=max(1, int(width * S)))

    def _txt(self, d, x, y, text, font, fill, anchor="la") -> None:
        d.text((x * S, y * S), text, font=font, fill=fill, anchor=anchor)

    def _seg_ring(self, d, cx, cy, r, width, color, segs, gap, rot) -> None:
        step = 360 / segs
        for k in range(segs):
            self._arc(d, cx, cy, r, rot + k * step + gap / 2,
                      rot + (k + 1) * step - gap / 2, width, color)

    # ---- 渲染循环 ----------------------------------------------------
    def _tick(self) -> None:
        try:
            while True:
                kind, val = self._q.get_nowait()
                if kind == "state":
                    self._state = val
                elif kind == "line":
                    self._push_line(*val)
        except queue.Empty:
            pass

        self._phase += 0.14
        img = self._render()
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfig(self._img_id, image=self._photo)
        self.root.after(70, self._tick)

    def _render(self) -> Image.Image:
        if self._bg is None:
            self._bg = self._build_bg()
        big = self._bg.copy()
        d = ImageDraw.Draw(big)
        color = STATE_COLOR.get(self._state, STATE_COLOR["idle"])

        self._draw_sweep(d)
        self._draw_header(d)
        self._draw_reactor(d, color)
        self._draw_waveform(d, color)
        self._draw_stats(d)
        self._draw_notes(d)
        self._draw_transcript(d, color)
        return big.resize((W, H), Image.LANCZOS)

    # ---- 静态背景层（只构建一次，每帧 copy 复用）------------------
    def _build_bg(self) -> Image.Image:
        big = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        m = 6
        d.rounded_rectangle([m * S, m * S, (W - m) * S, (H - m) * S],
                            radius=18 * S, fill=(3, 7, 9, 247))

        ix0, iy0, ix1, iy1 = m + 4, m + 4, W - m - 4, H - m - 4
        # 网格（极淡，仅做底纹）
        gx = ix0
        while gx < ix1:
            self._line(d, gx, iy0, gx, iy1, 1, (*GRID, 8))
            gx += 40
        gy = iy0
        while gy < iy1:
            self._line(d, ix0, gy, ix1, gy, 1, (*GRID, 8))
            gy += 40
        # CRT 扫描线（极淡，不压字）
        sy = iy0
        while sy < iy1:
            self._line(d, ix0, sy, ix1, sy, 1, (0, 12, 14, 8))
            sy += 4

        # 文字区凹陷底板：盖掉底纹、衬高对比，让字更清晰
        for x0, y0, x1, y1 in (
            (RX - 12, 112, RXE + 10, 466),     # 右栏 SYSTEM + NOTES
            (24, 470, W - 24, H - 32),          # 底部转写台账
        ):
            d.rounded_rectangle([x0 * S, y0 * S, x1 * S, y1 * S],
                                radius=9 * S, fill=(0, 4, 6, 165))

        # 面板描边 + 四角 L 装饰
        d.rounded_rectangle([m * S, m * S, (W - m) * S, (H - m) * S],
                            radius=18 * S, outline=(*TEAL_DIM, 210),
                            width=max(1, int(1.5 * S)))
        c = 24
        for cx, cy, sx, sy2 in ((m + 14, m + 14, 1, 1), (W - m - 14, m + 14, -1, 1),
                                (m + 14, H - m - 14, 1, -1),
                                (W - m - 14, H - m - 14, -1, -1)):
            self._line(d, cx, cy, cx + sx * c, cy, 2, (*TEAL, 235))
            self._line(d, cx, cy, cx, cy + sy2 * c, 2, (*TEAL, 235))

        # 分隔线
        self._line(d, 24, 104, W - 24, 104, 1, (*TEAL_DIM, 170))
        self._line(d, DIV_X, 116, DIV_X, 470, 1, (*TEAL_DIM, 150))

        # 静态区块标题 + 页脚
        self._txt(d, RX, 118, "▮ SYSTEM", _mono(11), (*TEAL, 235))
        self._txt(d, RX, 286, "▮ NOTES", _mono(11), (*TEAL, 235))
        self._txt(d, LX + 2, 384, "▮ AUDIO", _mono(10), (*TEAL_DIM, 230))
        self._txt(d, W // 2, H - 20,
                  "J A R V I S   D I S P L A Y   S Y S T E M",
                  _mono(10), (*TEAL_DIM, 235), anchor="ma")
        return big

    def _draw_sweep(self, d) -> None:
        """缓慢向下扫掠的高光线，增强 CRT 全息感。"""
        span = H - 24
        y = 12 + (self._phase * 6) % span
        for off, a in ((0, 26), (-2, 14), (2, 14)):
            self._line(d, 12, y + off, W - 12, y + off, 1, (*TEAL, a))

    # ---- 顶栏 --------------------------------------------------------
    def _draw_header(self, d) -> None:
        now = time.localtime()
        clock = time.strftime("%H:%M", now)
        months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL",
                  "AUG", "SEP", "OCT", "NOV", "DEC"]
        days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        date_str = f"{months[now.tm_mon - 1]} {now.tm_mday:02d}   {days[now.tm_wday]}"

        self._txt(d, 28, 24, clock, _mono(46), (*TEAL_HI, 255))
        tw = d.textlength(clock, font=_mono(46)) / S
        self._txt(d, 28 + tw + 8, 52, time.strftime("%S", now), _mono(16),
                  (*TEAL, 255))
        self._txt(d, 30, 82, date_str, _mono(13), (205, 240, 248, 255))

        temp = self._weather.temp or "--°"
        cond = self._weather.text or "SYS ONLINE"
        self._txt(d, RXE, 26, temp, _mono(32), (*TEAL_HI, 255), anchor="ra")
        self._txt(d, RXE, 62, cond[:18], _mono(12), (195, 232, 242, 245),
                  anchor="ra")

    # ---- 弧反应堆 ----------------------------------------------------
    def _draw_reactor(self, d, color) -> None:
        rr, gg, bb = color
        ph = self._phase
        spin = ph * (60 if self._state == "thinking" else 9)
        if self._state == "speaking":
            pulse = 1.0 + 0.05 * math.sin(ph * 2.4)
        elif self._state == "listening":
            pulse = 1.0 + 0.035 * math.sin(ph * 1.7)
        else:
            pulse = 1.0 + 0.03 * math.sin(ph * 0.9)
        R = REACTOR_R * pulse
        cx, cy = CXL, CYL

        for i in range(16, 0, -1):
            frac = i / 16
            self._ell(d, cx, cy, R * (1.0 + 0.42 * frac),
                      fill=(rr, gg, bb, int(26 * (1 - frac) ** 2)))
        self._seg_ring(d, cx, cy, R * 1.22, 2.5, (rr, gg, bb, 205),
                       segs=54, gap=2.2, rot=-spin * 0.4)
        self._seg_ring(d, cx, cy, R * 1.05, 6, (rr, gg, bb, 150),
                       segs=12, gap=9, rot=spin)
        self._ell(d, cx, cy, R * 0.92, outline=(rr, gg, bb, 220),
                  width=max(1, int(1.5 * S)))

        n = 10
        r_in, r_out = R * 0.42, R * 0.82
        for k in range(n):
            a = math.radians(k * 360 / n - spin * 0.25)
            aw = math.radians(360 / n * 0.36)
            pts = []
            for sign in (-1, 1):
                ang = a + sign * aw
                pts.append(((cx + math.cos(ang) * r_out) * S,
                            (cy + math.sin(ang) * r_out) * S))
            for sign in (1, -1):
                ang = a + sign * aw * 0.55
                pts.append(((cx + math.cos(ang) * r_in) * S,
                            (cy + math.sin(ang) * r_in) * S))
            d.polygon(pts, fill=(rr, gg, bb, 92), outline=(rr, gg, bb, 230))

        self._ell(d, cx, cy, R * 0.4, outline=(rr, gg, bb, 230),
                  width=max(1, int(1.2 * S)))
        core = 10
        for i in range(core, 0, -1):
            frac = i / core
            col = (int(rr + (255 - rr) * (1 - frac) ** 1.4),
                   int(gg + (255 - gg) * (1 - frac) ** 1.4),
                   int(bb + (255 - bb) * (1 - frac) ** 1.4), 255)
            self._ell(d, cx, cy, R * 0.32 * frac, fill=col)

        self._txt(d, cx, cy + R * 1.34, STATE_LABEL.get(self._state, "STANDBY"),
                  _mono(12), (rr, gg, bb, 255), anchor="ma")

    # ---- 音频波形 ----------------------------------------------------
    def _draw_waveform(self, d, color) -> None:
        rr, gg, bb = color
        x0, x1 = LX, DIV_X - 18
        midy = 428
        self._line(d, x0, midy, x1, midy, 1, (*TEAL_DIM, 120))

        amp = {"speaking": 30, "listening": 22, "thinking": 9}.get(self._state, 6)
        bars = 48
        step = (x1 - x0) / bars
        ph = self._phase
        for i in range(bars):
            v = (0.45 * math.sin(ph * 3.0 + i * 0.55)
                 + 0.3 * math.sin(ph * 5.3 + i * 0.27)
                 + 0.25 * math.sin(ph * 1.7 + i * 0.9))
            env = 0.55 + 0.45 * math.sin(ph * 2.0 + i * 0.4)  # 语音包络
            h = abs(v) * amp * (env if self._state in ("speaking", "listening")
                                else 1.0) + 1.5
            x = x0 + i * step + step / 2
            peak = abs(v) > 0.7
            col = (*(TEAL_HI if peak else color), 235)
            self._line(d, x, midy - h, x, midy + h, max(1.6, step * 0.42), col)

    # ---- 系统遥测 ----------------------------------------------------
    def _draw_stats(self, d) -> None:
        total, free = self._tele.disk()
        used = (total - free) / total if total else 0
        pct, charging = self._tele.battery()
        la, cpu = self._tele.load()
        rows = [
            ("DISK", f"{free:.0f}G FREE / {total:.0f}G", used, False),
            ("POWER", (f"{pct}%" + ("  CHG" if charging else ""))
             if pct is not None else "--", (pct or 0) / 100, True),
            ("CPU LOAD", f"{la:.2f}  {cpu * 100:.0f}%", cpu, False),
            ("UPTIME", self._tele.uptime(), None, False),
        ]
        x, w, y0 = RX, RXE - RX, 146
        for i, (label, val, frac, good_high) in enumerate(rows):
            y = y0 + i * 30
            self._txt(d, x, y, label, _mono(12), (120, 226, 240, 255))
            self._txt(d, x + w, y, val, _mono(13), (218, 244, 251, 255),
                      anchor="ra")
            if frac is not None:
                by = y + 17
                self._line(d, x, by, x + w, by, 2, (*TEAL_DIM, 110))
                fc = TEAL
                if (good_high and frac < 0.2) or (not good_high and frac > 0.85):
                    fc = (240, 150, 80)
                self._line(d, x, by, x + w * max(0.02, frac), by, 2, (*fc, 255))

    # ---- 笔记 / 待办 -------------------------------------------------
    def _draw_notes(self, d) -> None:
        font = _han(14)
        x, w, y = RX, RXE - RX, 312
        for item in self._notes.items():
            if y > 452:
                break
            self._txt(d, x, y + 2, "›", _mono(13), (120, 226, 240, 255))
            for ln in self._wrap(d, item, font, w - 16)[:2]:
                if y > 452:
                    break
                self._txt(d, x + 16, y, ln, font, (210, 240, 248, 255))
                y += 22
            y += 5

    # ---- 转写台账（底部通栏）----------------------------------------
    def _draw_transcript(self, d, color) -> None:
        bx0, by0, bx1, by1 = 24, 470, W - 24, H - 32
        d.rounded_rectangle([bx0 * S, by0 * S, bx1 * S, by1 * S], radius=9 * S,
                            outline=(*TEAL_DIM, 190), width=max(1, int(1 * S)))
        self._txt(d, bx0 + 12, by0 - 9, " TRANSCRIPT ", _mono(10),
                  (120, 226, 240, 255))

        font = _han(14)
        rendered: list[tuple[str, str]] = []
        for role, text in self._lines:
            prefix = {"you": "你 › ", "jarvis": "J › ", "sys": "» "}.get(role, "")
            for ln in self._wrap(d, prefix + text, font, bx1 - bx0 - 24):
                rendered.append((role, ln))
        lh = 22
        avail = int((by1 - by0 - 18) / lh)
        jc = tuple(int(c + (255 - c) * 0.35) for c in color)   # 回答色提亮
        rolecol = {"you": (205, 238, 247, 255), "jarvis": (*jc, 255),
                   "sys": (175, 215, 224, 240)}
        ty = by0 + 12
        for role, ln in rendered[-avail:]:
            self._txt(d, bx0 + 12, ty, ln, font, rolecol.get(role, TEAL))
            ty += lh

    def _wrap(self, d, text, font, max_w) -> list[str]:
        limit = max_w * S
        lines, cur = [], ""
        for ch in text:
            if ch == "\n" or d.textlength(cur + ch, font=font) > limit:
                if cur:
                    lines.append(cur)
                cur = "" if ch == "\n" else ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        return lines or [""]
