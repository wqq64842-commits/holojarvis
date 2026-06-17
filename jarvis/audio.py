"""麦克风采集 + 基于音量的简单断句(VAD)。

思路：持续读取小帧音频，计算音量(RMS)。音量超过阈值认为有人说话，开始录音；
句尾连续静音超过 SILENCE_TAIL 秒就判定一句说完，产出这段音频交给识别。
启动时自动采集一小段环境噪音来校准阈值。
"""

from __future__ import annotations

import queue
from collections.abc import Iterator

import numpy as np
import sounddevice as sd

from . import config

_FRAME = int(config.SAMPLE_RATE * config.FRAME_MS / 1000)   # 每帧采样点数


def _rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) + 1e-9)


class Microphone:
    """以 16k/单声道/int16 持续采集，产出一段段语音(float32, 归一化)。"""

    def __init__(self) -> None:
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._stream = sd.InputStream(
            samplerate=config.SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=_FRAME,
            callback=self._on_audio,
        )
        self.threshold = 500.0   # 会在 calibrate() 里更新
        self.on_speech_start = None   # 检测到有人开口时回调（给桌宠显示"聆听"用）

    def _on_audio(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        self._q.put(indata[:, 0].copy())

    def __enter__(self) -> "Microphone":
        self._stream.start()
        self.calibrate()
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self._stream.stop()
        self._stream.close()

    def flush(self) -> None:
        """清空缓冲队列——朗读完后调用，丢弃把自己声音录进去的那段音频，防止自言自语。"""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def calibrate(self, seconds: float = 1.0) -> None:
        """采集环境噪音，把阈值设为噪音的若干倍。"""
        levels = []
        need = int(seconds * 1000 / config.FRAME_MS)
        while len(levels) < need:
            levels.append(_rms(self._q.get()))
        floor = float(np.median(levels))
        self.threshold = max(floor * 3.5, 400.0)

    def _frames(self) -> Iterator[np.ndarray]:
        while True:
            yield self._q.get()

    def segments(self) -> Iterator[np.ndarray]:
        """阻塞式产出一段段语音（float32, [-1,1]）。"""
        tail = int(config.SILENCE_TAIL * 1000 / config.FRAME_MS)
        max_frames = int(config.MAX_SEGMENT * 1000 / config.FRAME_MS)
        min_frames = int(config.MIN_SPEECH * 1000 / config.FRAME_MS)

        buf: list[np.ndarray] = []
        silence = 0
        speaking = False

        for frame in self._frames():
            loud = _rms(frame) > self.threshold
            if speaking:
                buf.append(frame)
                silence = 0 if loud else silence + 1
                if silence >= tail or len(buf) >= max_frames:
                    if len(buf) >= min_frames:
                        audio = np.concatenate(buf).astype(np.float32) / 32768.0
                        yield audio
                    buf, silence, speaking = [], 0, False
            elif loud:
                speaking = True
                buf = [frame]
                silence = 0
                if self.on_speech_start:
                    self.on_speech_start()
