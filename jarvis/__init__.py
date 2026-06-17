"""贾维斯 (Jarvis) —— Mac 上的中文语音助手。

架构：
    麦克风采集 -> 本地 Whisper 识别 -> 唤醒词判断 -> Claude 大脑(工具调用) -> Mac say 朗读
"""

__version__ = "1.0.0"
