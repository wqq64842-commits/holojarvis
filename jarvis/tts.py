"""文字转语音。两种后端：
  - clone：调本地克隆音服务(voice_clone/serve.py)，用参考音色说话；服务没开则自动回退
  - say  ：macOS 自带 `say`（中文男声 Eddy），零依赖、即时
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request

from . import config

_proc: subprocess.Popen | None = None


def _clean(text: str) -> str:
    """去掉不适合朗读的 markdown 符号 / emoji，让朗读更自然。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)      # 代码块
    text = re.sub(r"[*_`#>\-]+", "", text)                  # markdown 标记
    text = re.sub(r"https?://\S+", "网址链接", text)        # URL
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", text)  # emoji
    return text.strip()


def _play_file(path: str, blocking: bool) -> None:
    global _proc
    cmd = ["afplay", path]
    if blocking:
        subprocess.run(cmd)
    else:
        _proc = subprocess.Popen(cmd)


def _speak_say(text: str, blocking: bool) -> None:
    global _proc
    cmd = ["say", "-v", config.TTS_VOICE, "-r", str(config.TTS_RATE), text]
    if blocking:
        subprocess.run(cmd)
    else:
        _proc = subprocess.Popen(cmd)


def _speak_clone(text: str, blocking: bool) -> bool:
    """请求克隆音服务合成并播放；成功返回 True，失败(服务没开等)返回 False。"""
    try:
        req = urllib.request.Request(
            config.VOICE_SERVER + "/tts",
            data=text.encode("utf-8"), method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        path = obj.get("path")
        if not path:
            return False
        _play_file(path, blocking)
        return True
    except Exception:  # noqa: BLE001
        return False


def _speak_gptsovits(text: str, blocking: bool) -> bool:
    """请求本地 GPT-SoVITS API(v2) 合成并播放；失败返回 False 以便回退。"""
    import tempfile
    payload = json.dumps({
        "text": text,
        "text_lang": config.GPTSOVITS_TEXT_LANG,
        "ref_audio_path": config.GPTSOVITS_REF,
        "prompt_text": config.GPTSOVITS_PROMPT,
        "prompt_lang": config.GPTSOVITS_PROMPT_LANG,
        "text_split_method": "cut5",
        "media_type": "wav",
        "streaming_mode": False,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            config.GPTSOVITS_URL + "/tts", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        if not data or len(data) < 100:        # 出错时多半返回的是 json 错误
            return False
        path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        with open(path, "wb") as f:
            f.write(data)
        _play_file(path, blocking)
        return True
    except Exception:  # noqa: BLE001
        return False


def speak(text: str, blocking: bool = True) -> None:
    """朗读文字。克隆后端不可用时自动回退到 say。"""
    text = _clean(text)
    if not text:
        return
    stop()  # 先打断上一句
    backend = config.TTS_BACKEND
    if backend == "gptsovits" and _speak_gptsovits(text, blocking):
        return
    if backend == "clone" and _speak_clone(text, blocking):
        return
    _speak_say(text, blocking)


def stop() -> None:
    """打断当前朗读。"""
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
    _proc = None
