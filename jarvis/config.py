"""配置与密钥加载。

API Key 查找顺序：
    1. 环境变量 ANTHROPIC_API_KEY
    2. 项目根目录下的 api_key.txt 文件（只放一行 key）
    3. ~/.jarvis_key 文件
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# ---- 平台判定（贾维斯支持 macOS 与 Windows）---------------------------
IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
OS_NAME = "Windows" if IS_WINDOWS else "macOS" if IS_MACOS else "Linux"

# ---- 可调参数 ----------------------------------------------------------


def _read_first_line(filename: str) -> str:
    """读项目根某文件第一行非空、非 # 注释的内容；没有就返回空串。"""
    p = _ROOT / filename
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return ""


# 大模型名（贾维斯的大脑）。优先级：环境变量 JARVIS_MODEL > model.txt > 默认。
# 走中转站时这里填中转站支持的模型名，如 deepseek-chat / gpt-4o / claude-...
MODEL = (os.environ.get("JARVIS_MODEL") or _read_first_line("model.txt")
         or "deepseek-chat").strip()
MAX_TOKENS = 1024

# 中转站（OpenAI 兼容网关）地址。优先级：环境变量 JARVIS_BASE_URL > base_url.txt。
# 例：https://你的中转站/v1   （贾维斯会自动在后面接 /chat/completions）
LLM_BASE_URL = (os.environ.get("JARVIS_BASE_URL")
                or _read_first_line("base_url.txt")).strip()


def llm_endpoint() -> str:
    """拼出 chat/completions 端点。base_url.txt 填到 /v1 即可。"""
    base = LLM_BASE_URL.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"

# Whisper 语音识别模型大小：tiny / base / small / medium
# small 对中文识别准确度和速度比较平衡；机器弱可改成 base
WHISPER_MODEL = os.environ.get("JARVIS_WHISPER", "small")
WHISPER_COMPUTE = "int8"          # CPU 上用 int8 最快
ASR_LANGUAGE = "zh"

# TTS 后端：
#   gptsovits = 调用本地 GPT-SoVITS API，用参考音色克隆说话（推荐，你已有此项目）
#   clone     = 用内置 XTTS 克隆音服务(voice_clone/serve.py)
#   say       = 系统自带嗓音（macOS 的 say / Windows 的 SAPI 语音合成）
# 任何克隆后端连不上时都会自动回退到系统嗓音。
TTS_BACKEND = os.environ.get("JARVIS_TTS", "gptsovits")
VOICE_SERVER = os.environ.get("JARVIS_VOICE_SERVER", "http://127.0.0.1:5111")

# ---- GPT-SoVITS 后端参数 ----
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GPTSOVITS_URL = os.environ.get("GPTSOVITS_URL", "http://127.0.0.1:9880")
# 参考音色 wav（决定贾维斯的嗓音）+ 它对应的文字
GPTSOVITS_REF = os.environ.get(
    "GPTSOVITS_REF", os.path.join(_ROOT_DIR, "jarvis_ref.wav"))
GPTSOVITS_PROMPT = os.environ.get(
    "GPTSOVITS_PROMPT",
    "在一段时间中，让我告诉你一个故事，这里有一个很有趣的东西。")
GPTSOVITS_TEXT_LANG = os.environ.get("GPTSOVITS_TEXT_LANG", "zh")
GPTSOVITS_PROMPT_LANG = os.environ.get("GPTSOVITS_PROMPT_LANG", "zh")

# say 后端的声音（`say -v '?'` 可查看全部）。
# 默认中文男声 Eddy；想换成婷婷(女声)设 JARVIS_VOICE=Tingting。
TTS_VOICE = os.environ.get("JARVIS_VOICE", "Eddy")
TTS_RATE = int(os.environ.get("JARVIS_RATE", "190"))   # 语速，字/分钟

# 唤醒词（及 Whisper 常见的同音误写变体，做模糊匹配用）
WAKE_WORDS = [
    "贾维斯", "贾维斯", "杰维斯", "佳维斯", "嘉维斯", "假维斯",
    "贾威斯", "加维斯", "甲维斯", "jarvis",
]

# 唤醒后保持「清醒」可继续对话的时长（秒）；超时无话则回到待机
ACTIVE_TIMEOUT = 25

# ---- 防误唤醒（噪音/电视声）-------------------------------------------
# 待机时，只有同时满足下面两个置信度条件的识别结果才会去判断唤醒词，
# 借此过滤掉 Whisper 对噪音/背景人声产生的「幻听」。
WAKE_MAX_NO_SPEECH = 0.5     # 无语音概率高于此值 → 判为噪音，丢弃
WAKE_MIN_LOGPROB = -1.0      # 识别置信度低于此值 → 判为噪音，丢弃
WAKE_MIN_LEN = 3            # 唤醒句太短(<3字)多半是误识别，丢弃
WAKE_SIM = 0.8             # 唤醒词拼音相似度阈值(0~1)，越大越严；总不灵可降到 0.72

# ---- 音频参数 ----------------------------------------------------------

SAMPLE_RATE = 16000               # Whisper 要求 16k
FRAME_MS = 30                     # 每帧时长
SILENCE_TAIL = 0.7                # 句尾静音多久判定说完（秒）
MIN_SPEECH = 0.3                  # 太短的声音(<0.3s)忽略，多半是噪音
MAX_SEGMENT = 15                  # 单段录音上限（秒）

# ---- 密钥 --------------------------------------------------------------


def load_api_key() -> str | None:
    """中转站（或任意 LLM 后端）的 API Key。
    优先级：环境变量 JARVIS_API_KEY > ANTHROPIC_API_KEY > api_key.txt > ~/.jarvis_key。
    走中转站时，把中转站的 key 填进 api_key.txt 即可。"""
    for var in ("JARVIS_API_KEY", "ANTHROPIC_API_KEY"):
        key = os.environ.get(var)
        if key and key.strip():
            return key.strip()
    for path in (_ROOT / "api_key.txt", Path.home() / ".jarvis_key"):
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    return None
