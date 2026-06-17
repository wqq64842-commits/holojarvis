"""贾维斯克隆语音服务（独立进程，跑在 .venv-tts 里）。

用 XTTS-v2 做零样本克隆：以参考音频的音色，把文字合成成语音。
模型只在启动时加载一次（约 1.8GB，首次会自动下载），之后每次请求快速合成。

接口（本地 HTTP）：
  GET /health            -> ok
  POST /tts  body=文字   -> {"path": "/tmp/xxx.wav"}   合成好的 wav 路径

环境变量：
  JARVIS_VOICE_REF   参考音频路径（决定音色）
  JARVIS_VOICE_PORT  端口（默认 5111）
  JARVIS_VOICE_LANG  语言（默认 zh-cn）
  JARVIS_VOICE_DEVICE cpu/mps（默认 cpu，最稳）
"""

from __future__ import annotations

import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("COQUI_TOS_AGREED", "1")   # 跳过 XTTS 许可交互提示

REF = os.environ.get("JARVIS_VOICE_REF",
                     "/Users/shu/Desktop/声音/20260610_223257.wav")
PORT = int(os.environ.get("JARVIS_VOICE_PORT", "5111"))
LANG = os.environ.get("JARVIS_VOICE_LANG", "zh-cn")
DEVICE = os.environ.get("JARVIS_VOICE_DEVICE", "cpu")

_tts = None


def _load():
    global _tts
    if _tts is not None:
        return
    print("⏳ 正在加载 XTTS-v2 克隆模型（首次会下载约 1.8GB）…", flush=True)
    from TTS.api import TTS
    _tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(DEVICE)
    print(f"✓ 克隆模型就绪（device={DEVICE}，音色参考={REF}）", flush=True)


def _split(text: str, limit: int = 200):
    """XTTS 单次文本不宜过长，按标点粗分句。"""
    import re
    parts, cur = [], ""
    for seg in re.split(r"(?<=[。！？.!?；;\n])", text):
        if len(cur) + len(seg) > limit and cur:
            parts.append(cur)
            cur = seg
        else:
            cur += seg
    if cur.strip():
        parts.append(cur)
    return parts or [text]


def synth(text: str) -> str:
    _load()
    chunks = _split(text)
    out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    if len(chunks) == 1:
        _tts.tts_to_file(text=chunks[0], speaker_wav=REF, language=LANG,
                         file_path=out)
        return out
    # 多句：逐句合成再拼接
    import wave
    paths = []
    for c in chunks:
        p = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        _tts.tts_to_file(text=c, speaker_wav=REF, language=LANG, file_path=p)
        paths.append(p)
    with wave.open(out, "wb") as w:
        for i, p in enumerate(paths):
            with wave.open(p, "rb") as r:
                if i == 0:
                    w.setparams(r.getparams())
                w.writeframes(r.readframes(r.getnframes()))
            os.remove(p)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音访问日志
        pass

    def do_GET(self):
        if self.path == "/health":
            self._json({"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/tts":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        text = self.rfile.read(n).decode("utf-8").strip()
        if not text:
            self._json({"error": "empty"})
            return
        try:
            path = synth(text)
            self._json({"path": path})
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)})

    def _json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    _load()   # 启动即加载，避免首句卡顿
    print(f"🔊 克隆语音服务监听 http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
