"""持久记忆——让贾维斯跨重启记住关于你的事（"越用越懂你"）。

存成项目根目录下的 memory.json。贾维斯通过 remember/forget 工具增删，
启动时这些记忆会注入到系统提示里，于是它"开机就记得你"。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "memory.json"


def load() -> list[dict]:
    if _PATH.exists():
        try:
            return json.loads(_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save(items: list[dict]) -> None:
    _PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                     encoding="utf-8")


def add(fact: str) -> str:
    fact = (fact or "").strip()
    if not fact:
        return "（没有要记的内容）"
    items = load()
    if any(it.get("fact") == fact for it in items):
        return "这个我早就记下了"
    items.append({"fact": fact, "at": time.strftime("%Y-%m-%d")})
    _save(items)
    return f"好的，我记住了：{fact}"


def forget(keyword: str) -> str:
    keyword = (keyword or "").strip()
    items = load()
    kept = [it for it in items if keyword not in it.get("fact", "")]
    removed = len(items) - len(kept)
    if removed:
        _save(kept)
        return f"已经忘掉 {removed} 条相关记忆"
    return "没找到相关的记忆"


def as_prompt() -> str:
    """把所有记忆拼成一段，注入系统提示。"""
    items = load()
    if not items:
        return ""
    lines = "\n".join(f"- {it.get('fact', '')}" for it in items)
    return ("\n\n# 关于用户你已经记住的事（自然地运用，别刻意复述）：\n" + lines)
