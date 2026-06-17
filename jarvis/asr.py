"""语音识别：本地 faster-whisper（离线、免费、中文效果好）。"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np
from faster_whisper import WhisperModel

from . import config

_model: WhisperModel | None = None


class ASRResult(NamedTuple):
    text: str
    no_speech: float       # 越大越像噪音/无语音
    avg_logprob: float     # 越大越可信


def load() -> None:
    """加载模型（首次运行会自动下载，存到 ~/.cache）。"""
    global _model
    if _model is None:
        _model = WhisperModel(
            config.WHISPER_MODEL,
            device="cpu",
            compute_type=config.WHISPER_COMPUTE,
        )


def transcribe(audio: np.ndarray) -> ASRResult:
    """把一段音频转成中文文本，并附带置信度信息。"""
    if _model is None:
        load()
    assert _model is not None
    segments, _ = _model.transcribe(
        audio,
        language=config.ASR_LANGUAGE,
        beam_size=5,
        vad_filter=True,                       # 再过滤一遍静音，更稳
    )
    segs = list(segments)
    text = "".join(s.text for s in segs).strip()
    if not segs:
        return ASRResult("", 1.0, -10.0)
    no_speech = sum(s.no_speech_prob for s in segs) / len(segs)
    avg_logprob = sum(s.avg_logprob for s in segs) / len(segs)
    return ASRResult(text, no_speech, avg_logprob)
