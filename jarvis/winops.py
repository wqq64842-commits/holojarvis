"""Windows 平台的底层操作实现（仅在 Windows 上被调用）。

贾维斯本来是 macOS 上的助手，靠 osascript / say / afplay / pmset 等命令做事。
本模块用 **标准库 + PowerShell + ctypes** 给出这些能力的 Windows 等价实现，
不引入额外依赖（不需要 pywin32）。

注意：本模块在导入时不会触碰任何 Windows-only 的接口（ctypes.windll、winsound 等
都只在函数内部按需调用），所以即使在 macOS 上被 import 也不会报错——只是不该被调用。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
import time

# 让 PowerShell 子进程不弹黑窗
_NO_WINDOW = 0x08000000


# ---- PowerShell 执行器 -------------------------------------------------

def powershell(script: str, env: dict | None = None, timeout: float = 60) -> str:
    """跑一段 PowerShell，返回 stdout（UTF-8）。出错时返回 stderr。"""
    full = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + script
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", full],
            capture_output=True, env=env, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return "（命令超时）"
    out = r.stdout.decode("utf-8", "ignore").strip()
    if not out:
        out = r.stderr.decode("utf-8", "ignore").strip()
    return out


# ---- 剪贴板 ------------------------------------------------------------

def get_clipboard() -> str:
    return powershell("Get-Clipboard -Raw")


def set_clipboard(text: str) -> None:
    # 经环境变量传值，免去引号转义的烦恼
    powershell("Set-Clipboard -Value $env:JV_CLIP",
               env={**os.environ, "JV_CLIP": text})


# ---- 按键模拟（虚拟键码，控制媒体/音量）-------------------------------

_VK = {
    "media_play_pause": 0xB3, "media_next": 0xB0, "media_prev": 0xB1,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
}
_KEYEVENTF_KEYUP = 0x0002


def _tap(vk: int, times: int = 1) -> None:
    user32 = ctypes.windll.user32
    for _ in range(max(1, times)):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        time.sleep(0.005)


def media(action: str) -> str:
    """控制系统媒体播放（对当前播放器/网页音乐生效）。"""
    names = {"play": "播放", "pause": "暂停", "playpause": "切换播放",
             "next": "下一首", "previous": "上一首"}
    if action in ("play", "pause", "playpause"):
        _tap(_VK["media_play_pause"])
    elif action == "next":
        _tap(_VK["media_next"])
    elif action == "previous":
        _tap(_VK["media_prev"])
    return names.get(action, "已操作") + "音乐"


def set_volume(level: int) -> str:
    """设置系统主音量（近似）。Windows 无简单的"设到绝对值"命令，
    这里先按音量键降到底，再按上调到目标档位（每次约 2%）。"""
    level = max(0, min(100, int(level)))
    _tap(_VK["volume_down"], 50)          # 先归零
    _tap(_VK["volume_up"], round(level / 2))   # 每次约 2%，升到目标
    return f"音量已设为约 {level}"


# ---- 应用 / 电源 -------------------------------------------------------

# 常见中文应用名 → Windows 上的可执行名/启动方式
_APP_ALIASES = {
    "微信": "weixin", "wechat": "weixin",
    "浏览器": "msedge", "edge": "msedge", "chrome": "chrome", "safari": "msedge",
    "计算器": "calc", "记事本": "notepad", "备忘录": "notepad",
    "文件管理器": "explorer", "访达": "explorer", "finder": "explorer",
    "music": "wmplayer", "音乐": "wmplayer", "设置": "ms-settings:",
    "任务管理器": "taskmgr", "画图": "mspaint", "终端": "wt",
    "cmd": "cmd", "powershell": "powershell",
}


def open_app(name: str) -> str:
    target = _APP_ALIASES.get(name.strip().lower(), name)
    try:
        # start 能按 PATH 上的可执行名 / 已注册的应用 / 协议来启动
        subprocess.run(["cmd", "/c", "start", "", target],
                       creationflags=_NO_WINDOW, timeout=10)
        return f"已打开 {name}"
    except Exception:  # noqa: BLE001
        return f"没找到应用「{name}」"


def lock() -> None:
    ctypes.windll.user32.LockWorkStation()


def sleep_pc() -> None:
    # 让系统进入睡眠（若禁用了睡眠会改为休眠）
    subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                   creationflags=_NO_WINDOW)


# ---- 回收站（比 rm 安全，可恢复）--------------------------------------

def recycle(path: str) -> str:
    """把文件/文件夹移到回收站，返回空串表示成功，否则返回错误信息。"""
    script = (
        "Add-Type -AssemblyName Microsoft.VisualBasic;"
        "$p=$env:JV_PATH;"
        "if(Test-Path -LiteralPath $p -PathType Container){"
        "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
        "$p,'OnlyErrorDialogs','SendToRecycleBin')}"
        "else{[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
        "$p,'OnlyErrorDialogs','SendToRecycleBin')}"
    )
    return powershell(script, env={**os.environ, "JV_PATH": path})


# ---- 微信发消息（UI 自动化）------------------------------------------

def send_wechat(contact: str, message: str) -> str:
    """激活微信 → Ctrl+F 搜索联系人 → 回车进会话 → 粘贴消息 → 回车发送。
    需微信已登录。中文用剪贴板粘贴以保证可靠；事后还原剪贴板。"""
    saved = get_clipboard()
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$w=New-Object -ComObject WScript.Shell;"
        "if(-not $w.AppActivate('微信')){[void]$w.AppActivate('WeChat')};"
        "Start-Sleep -Milliseconds 800;"
        "[System.Windows.Forms.SendKeys]::SendWait('^f');"
        "Start-Sleep -Milliseconds 500;"
        "Set-Clipboard -Value $env:JV_CONTACT;"
        "[System.Windows.Forms.SendKeys]::SendWait('^v');"
        "Start-Sleep -Milliseconds 1000;"
        "[System.Windows.Forms.SendKeys]::SendWait('{ENTER}');"
        "Start-Sleep -Milliseconds 800;"
        "Set-Clipboard -Value $env:JV_MSG;"
        "[System.Windows.Forms.SendKeys]::SendWait('^v');"
        "Start-Sleep -Milliseconds 400;"
        "[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')"
    )
    try:
        powershell(script, env={**os.environ,
                                "JV_CONTACT": contact, "JV_MSG": message})
        return f"已尝试给「{contact}」发送：{message}"
    finally:
        time.sleep(0.3)
        set_clipboard(saved)


# ---- 系统遥测（给桌宠 HUD 用）----------------------------------------

def boot_epoch() -> float:
    """开机时刻（Unix 时间戳）。用 GetTickCount64（毫秒）反推。"""
    tick_ms = ctypes.windll.kernel32.GetTickCount64()
    return time.time() - tick_ms / 1000.0


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def battery() -> tuple[int | None, bool]:
    """返回 (电量百分比 or None, 是否在充电)。"""
    status = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None, False
    pct = status.BatteryLifePercent
    pct = None if pct == 255 else int(pct)           # 255 = 未知/无电池
    charging = status.ACLineStatus == 1
    return pct, charging


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    @property
    def value(self) -> int:
        return (self.high << 32) | self.low


class CpuSampler:
    """用 GetSystemTimes 两次采样之差算 CPU 占用率（0~1）。"""

    def __init__(self) -> None:
        self._prev: tuple[int, int, int] | None = None

    def _read(self) -> tuple[int, int, int]:
        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        return idle.value, kernel.value, user.value

    def percent(self) -> float:
        try:
            idle, kernel, user = self._read()
        except Exception:  # noqa: BLE001
            return 0.0
        if self._prev is None:
            self._prev = (idle, kernel, user)
            return 0.0
        pi, pk, pu = self._prev
        self._prev = (idle, kernel, user)
        total = (kernel - pk) + (user - pu)   # kernel 已包含 idle
        if total <= 0:
            return 0.0
        busy = total - (idle - pi)
        return max(0.0, min(1.0, busy / total))
