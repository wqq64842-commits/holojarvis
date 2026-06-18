"""贾维斯能调用的「手脚」——控制电脑的各种工具（macOS / Windows 通用）。

每个工具都有：
    - 一个 Claude 可识别的 JSON schema（放进 TOOL_SCHEMAS）
    - 一个实际执行的 Python 函数（放进 DISPATCH）

平台差异：macOS 走 osascript / open / pmset 等；Windows 走 PowerShell / ctypes
（实现集中在 winops.py）。两边对外暴露同一套工具，大脑无需关心底层差异。
"""

from __future__ import annotations

import base64
import datetime
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from . import config, memory, tts

if config.IS_WINDOWS:
    from . import winops

# --- 各工具实现 --------------------------------------------------------


def _osascript(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()


def _key(combo: str) -> None:
    """发送一个按键组合给系统，例如 'command down' + 'v'。"""
    _osascript(f'tell application "System Events" to {combo}')


def _get_clipboard() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def _set_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"))


def open_app(name: str) -> str:
    if config.IS_WINDOWS:
        return winops.open_app(name)
    r = subprocess.run(["open", "-a", name], capture_output=True, text=True)
    return f"已打开 {name}" if r.returncode == 0 else f"没找到应用「{name}」"


def open_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if config.IS_WINDOWS:
        os.startfile(url)  # noqa: S606 — Windows 默认浏览器打开
    else:
        subprocess.run(["open", url])
    return "已在浏览器打开"


def web_search(query: str) -> str:
    q = urllib.parse.quote(query)
    url = f"https://www.bing.com/search?q={q}"
    if config.IS_WINDOWS:
        os.startfile(url)  # noqa: S606
    else:
        subprocess.run(["open", url])
    return f"已帮你搜索「{query}」"


def set_volume(level: int) -> str:
    if config.IS_WINDOWS:
        return winops.set_volume(level)
    level = max(0, min(100, int(level)))
    _osascript(f"set volume output volume {level}")
    return f"音量已设为 {level}"


def get_time() -> str:
    now = datetime.datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return now.strftime(f"现在是 %Y年%m月%d日 星期{week} %H点%M分")


def get_weather(city: str) -> str:
    """用 wttr.in 查天气（无需 API key）。"""
    try:
        c = urllib.parse.quote(city)
        fmt = urllib.parse.quote("%l：%C，%t，体感%f，湿度%h")
        url = f"https://wttr.in/{c}?format={fmt}&lang=zh"
        req = urllib.request.Request(url, headers={"User-Agent": "curl"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception as e:  # noqa: BLE001
        return f"查天气失败：{e}"


def control_music(action: str) -> str:
    if config.IS_WINDOWS:
        return winops.media(action)
    mapping = {
        "play": "play", "pause": "pause", "playpause": "playpause",
        "next": "next track", "previous": "previous track",
    }
    cmd = mapping.get(action, "playpause")
    _osascript(f'tell application "Music" to {cmd}')
    names = {"play": "播放", "pause": "暂停", "playpause": "切换播放",
             "next": "下一首", "previous": "上一首"}
    return names.get(action, "已操作") + "音乐"


def set_timer(seconds: int, message: str = "时间到") -> str:
    def fire() -> None:
        tts.speak(message, blocking=False)

    threading.Timer(max(1, int(seconds)), fire).start()
    mins = seconds // 60
    desc = f"{mins}分钟" if mins else f"{seconds}秒"
    return f"好的，{desc}后提醒你：{message}"


def take_screenshot() -> str:
    name = datetime.datetime.now().strftime("截图-%Y%m%d-%H%M%S.png")
    path = os.path.join(os.path.expanduser("~/Desktop"), name)
    if config.IS_WINDOWS:
        from PIL import ImageGrab
        ImageGrab.grab(all_screens=True).save(path)
    else:
        subprocess.run(["screencapture", path])
    return "截图已保存到桌面"


def system_power(action: str) -> str:
    if config.IS_WINDOWS:
        if action == "lock":
            winops.lock()
            return "已锁屏"
        if action == "sleep":
            winops.sleep_pc()
            return "电脑准备休眠"
        return "为安全起见，关机/重启请手动操作"
    if action == "lock":
        _osascript('tell application "System Events" to keystroke "q" using {control down, command down}')
        return "已锁屏"
    if action == "sleep":
        subprocess.run(["pmset", "sleepnow"])
        return "电脑准备休眠"
    return "为安全起见，关机/重启请手动操作"


def read_screen() -> list:
    """截取当前屏幕，把图片回传给大脑，让它"看"屏幕并总结/回答。

    返回的是一个内容块列表（含 image），会作为工具结果直接喂给 Claude 视觉。
    """
    if config.IS_WINDOWS:
        import tempfile
        from PIL import ImageGrab
        path = os.path.join(tempfile.gettempdir(), "jarvis_screen.jpg")
        try:
            img = ImageGrab.grab()
            img.thumbnail((1568, 1568))            # 长边 1568px，省 token
            img.convert("RGB").save(path, "JPEG", quality=80)
        except Exception as e:  # noqa: BLE001
            return f"截屏失败：{e}"
    else:
        path = "/tmp/jarvis_screen.jpg"
        # -x 静音截图，-m 只截主显示器，截成 jpg
        subprocess.run(["screencapture", "-x", "-m", "-t", "jpg", path],
                       capture_output=True)
        if not os.path.exists(path):
            return "截屏失败，请检查「屏幕录制」权限。"
        # 缩放到长边 1568px（Claude 视觉的最佳尺寸，省 token）
        subprocess.run(["sips", "-Z", "1568", path], capture_output=True)
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return [
        {"type": "text", "text": "这是用户当前的屏幕截图，请据此回答："},
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": data}},
    ]


def send_wechat(contact: str, message: str) -> str:
    """给微信联系人发消息。

    原理：用 UI 自动化操作 Mac 版微信——激活窗口 → Cmd+F 搜索联系人 →
    回车打开会话 → 粘贴消息 → 回车发送。中文用剪贴板粘贴以保证可靠。
    需要"辅助功能"权限，且微信已登录。Windows 上走 winops.send_wechat。
    """
    if config.IS_WINDOWS:
        return winops.send_wechat(contact, message)
    saved = _get_clipboard()                       # 备份剪贴板，事后还原
    try:
        _osascript('tell application "WeChat" to activate')
        time.sleep(0.8)
        _key('keystroke "f" using command down')   # 打开搜索
        time.sleep(0.5)
        _set_clipboard(contact)
        _key('keystroke "v" using command down')   # 粘贴联系人名
        time.sleep(1.0)
        _key("key code 36")                        # 回车，打开最匹配的会话
        time.sleep(0.8)
        _set_clipboard(message)
        _key('keystroke "v" using command down')   # 粘贴消息
        time.sleep(0.4)
        _key("key code 36")                        # 回车，发送
        time.sleep(0.3)
        return f"已尝试给「{contact}」发送：{message}"
    finally:
        time.sleep(0.3)
        _set_clipboard(saved)


def remember(fact: str) -> str:
    """把关于用户的一条事实/偏好写入长期记忆。"""
    return memory.add(fact)


def forget(keyword: str) -> str:
    """删除含某关键词的长期记忆。"""
    return memory.forget(keyword)


# --- 多步任务：文件 / 命令行 ------------------------------------------

def list_directory(path: str = "~") -> str:
    """列出目录内容（给多步文件任务用）。"""
    p = os.path.expanduser(path)
    if not os.path.isdir(p):
        return f"目录不存在：{path}"
    entries = []
    for name in sorted(os.listdir(p))[:200]:
        full = os.path.join(p, name)
        entries.append(f"{'📁' if os.path.isdir(full) else '📄'} {name}")
    return f"{p} 共 {len(entries)} 项：\n" + "\n".join(entries)


def run_shell(command: str) -> str:
    """执行一条系统命令并返回输出（多步任务的万能手段）。

    macOS 走 shell(zsh)，Windows 走 PowerShell。
    危险/批量/删除类操作应先经用户确认；删除请用 move_to_trash 而非 rm/del。
    """
    try:
        if config.IS_WINDOWS:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 command],
                capture_output=True, timeout=60, creationflags=0x08000000)
            out = (r.stdout.decode("utf-8", "ignore")
                   + r.stderr.decode("utf-8", "ignore")).strip()
        else:
            r = subprocess.run(command, shell=True, capture_output=True,
                               text=True, timeout=60)
            out = (r.stdout + r.stderr).strip()
        if len(out) > 2000:
            out = out[:2000] + "\n…(输出已截断)"
        return out or f"（命令已执行，无输出，退出码 {r.returncode}）"
    except subprocess.TimeoutExpired:
        return "命令超时（超过 60 秒）已中止"
    except Exception as e:  # noqa: BLE001
        return f"执行出错：{e}"


def move_to_trash(path: str) -> str:
    """把文件/文件夹移到废纸篓/回收站（比 rm/del 安全，可恢复）。"""
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return f"路径不存在：{path}"
    if config.IS_WINDOWS:
        err = winops.recycle(p)
        return f"已把「{os.path.basename(p)}」移到回收站" if not err \
            else f"移动失败：{err}"
    posix = p.replace('"', '\\"')
    out = _osascript(
        f'tell application "Finder" to delete (POSIX file "{posix}" as alias)'
    )
    return f"已把「{os.path.basename(p)}」移到废纸篓" if "error" not in out.lower() \
        else f"移动失败：{out}"


# --- 给 Claude 看的工具说明 -------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "open_app",
        "description": "打开一个应用程序，例如 微信、浏览器、备忘录/记事本、计算器、音乐。",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "应用名称"}},
            "required": ["name"],
        },
    },
    {
        "name": "open_url",
        "description": "在默认浏览器打开一个网址。",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "web_search",
        "description": "在浏览器里搜索某个关键词。",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "set_volume",
        "description": "设置系统音量，范围 0 到 100。",
        "input_schema": {
            "type": "object",
            "properties": {"level": {"type": "integer"}},
            "required": ["level"],
        },
    },
    {
        "name": "get_time",
        "description": "获取当前的日期和时间。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_weather",
        "description": "查询某个城市的天气，城市名用拼音或英文，例如 Beijing、Shanghai。",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "control_music",
        "description": "控制 Music 应用：play 播放, pause 暂停, playpause 切换, next 下一首, previous 上一首。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "pause", "playpause", "next", "previous"],
                }
            },
            "required": ["action"],
        },
    },
    {
        "name": "set_timer",
        "description": "设置一个倒计时提醒，到点用语音提醒用户。",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "description": "倒计时秒数"},
                "message": {"type": "string", "description": "到点要说的提醒内容"},
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "take_screenshot",
        "description": "截取当前屏幕并保存到桌面（只是存文件，不分析内容）。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_screen",
        "description": "看用户当前的屏幕内容。当用户问『屏幕上是什么』『帮我总结一下这个页面/这段』『这是什么意思』等需要看屏幕才能回答的问题时调用，调用后你会收到屏幕截图。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_wechat",
        "description": "用微信给某个联系人发送一条文字消息（需微信已登录并能被唤起到前台）。调用前务必已向用户口头确认『发给谁、发什么内容』。",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "联系人备注名或昵称"},
                "message": {"type": "string", "description": "要发送的消息内容"},
            },
            "required": ["contact", "message"],
        },
    },
    {
        "name": "system_power",
        "description": "电源/锁屏操作：lock 锁屏, sleep 休眠。关机重启不支持。",
        "input_schema": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["lock", "sleep"]}},
            "required": ["action"],
        },
    },
    {
        "name": "remember",
        "description": "把关于用户的事实或偏好写入长期记忆，跨重启永久记住。当用户透露自己的名字、喜好、习惯、常用设置、重要信息时主动调用。",
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string", "description": "要记住的一句话事实，如『用户叫小明』『用户喜欢安静』"}},
            "required": ["fact"],
        },
    },
    {
        "name": "forget",
        "description": "删除长期记忆中含某关键词的条目。",
        "input_schema": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
        },
    },
    {
        "name": "list_directory",
        "description": "列出某个目录下的文件和子文件夹。做多步文件任务时先用它了解现状。",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "目录路径，支持 ~，如 ~/Downloads"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_shell",
        "description": "执行一条系统命令并返回输出（macOS 为 shell/zsh，Windows 为 PowerShell），用于多步任务（建文件夹、批量移动/重命名、查询等）。注意：删除文件请改用 move_to_trash；批量或有风险的操作请先向用户口头确认再执行。",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "move_to_trash",
        "description": "把指定文件或文件夹移到废纸篓（可恢复，比 rm 安全）。删除操作一律用它。",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]

DISPATCH = {
    "open_app": open_app,
    "open_url": open_url,
    "web_search": web_search,
    "set_volume": set_volume,
    "get_time": get_time,
    "get_weather": get_weather,
    "control_music": control_music,
    "set_timer": set_timer,
    "take_screenshot": take_screenshot,
    "read_screen": read_screen,
    "send_wechat": send_wechat,
    "system_power": system_power,
    "remember": remember,
    "forget": forget,
    "list_directory": list_directory,
    "run_shell": run_shell,
    "move_to_trash": move_to_trash,
}


def run(name: str, args: dict) -> str:
    """执行某个工具，返回结果文本。"""
    fn = DISPATCH.get(name)
    if not fn:
        return f"未知工具：{name}"
    try:
        return fn(**args)
    except Exception as e:  # noqa: BLE001
        return f"执行 {name} 出错：{e}"
