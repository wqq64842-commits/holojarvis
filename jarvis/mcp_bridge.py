"""MCP 桥接——把本地 MCP 服务器的工具接进贾维斯的大脑。

读取项目根目录的 mcp.json，启动里面配置的 MCP 服务器(stdio)，把它们的工具
转成 Claude 能调用的工具(名字加 mcp__<服务器>__<工具> 前缀)。

MCP SDK 是 asyncio 的，这里用一个独立事件循环线程承载，对外暴露同步接口，
方便在贾维斯的同步主循环里调用。整个模块对错误高度容忍：装没装 SDK、配置在不在、
某个服务器起没起来，都不影响主程序——起得来几个用几个。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
from contextlib import AsyncExitStack
from pathlib import Path

from . import config

_CONFIG = Path(__file__).resolve().parent.parent / "mcp.json"


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:60]


def _resolve_command(cmd: str) -> str:
    """Windows 上 npx/npm/uvx 等是 .cmd/.exe，裸名字 subprocess 起不来，
    这里解析成可执行文件的绝对路径（解析不到就原样返回）。"""
    if not config.IS_WINDOWS or os.path.splitext(cmd)[1]:
        return cmd
    for cand in (cmd, cmd + ".cmd", cmd + ".exe", cmd + ".bat"):
        found = shutil.which(cand)
        if found:
            return found
    return cmd


def load_config() -> dict:
    if not _CONFIG.exists():
        return {}
    try:
        return json.loads(_CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


class McpBridge:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stack: AsyncExitStack | None = None
        self._schemas: list[dict] = []
        self._dispatch: dict[str, tuple] = {}   # full_name -> (session, tool_name)
        self._ready = threading.Event()
        self.names: list[str] = []              # 成功连上的服务器名

    # ---- 启动 --------------------------------------------------------
    def start(self, config: dict, log=print, timeout: float = 60) -> None:
        if not config:
            return
        try:
            import mcp  # noqa: F401
        except ImportError:
            log("⚠ 未安装 mcp 库，跳过 MCP（pip install mcp 可启用）")
            return
        threading.Thread(target=self._run, args=(config, log),
                         daemon=True).start()
        self._ready.wait(timeout=timeout)

    def _run(self, config: dict, log) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._setup(config, log))
        finally:
            self._ready.set()
        self._loop.run_forever()

    async def _setup(self, config: dict, log) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        for name, conf in config.items():
            if not conf.get("enabled", True):
                continue
            try:
                args = [os.path.expanduser(a) for a in conf.get("args", [])]
                env = {**os.environ, **conf.get("env", {})}
                # 绕开损坏的 ~/.npm 权限：给 npx 用项目内可写缓存
                if "npx" in conf["command"] and "npm_config_cache" not in env:
                    cache = _CONFIG.parent / ".npm-cache"
                    cache.mkdir(exist_ok=True)
                    env["npm_config_cache"] = str(cache)
                params = StdioServerParameters(
                    command=_resolve_command(conf["command"]), args=args, env=env,
                )
                read, write = await self._stack.enter_async_context(
                    stdio_client(params))
                session = await self._stack.enter_async_context(
                    ClientSession(read, write))
                await session.initialize()
                resp = await session.list_tools()
                for t in resp.tools:
                    full = f"mcp__{_sanitize(name)}__{_sanitize(t.name)}"[:64]
                    self._schemas.append({
                        "name": full,
                        "description": (t.description or t.name)[:1000],
                        "input_schema": t.inputSchema or {
                            "type": "object", "properties": {}},
                    })
                    self._dispatch[full] = (session, t.name)
                self.names.append(name)
                log(f"  ✓ MCP「{name}」已接入（{len(resp.tools)} 个工具）")
            except Exception as e:  # noqa: BLE001
                log(f"  ⚠ MCP「{name}」启动失败：{e}")

    # ---- 对外接口 ----------------------------------------------------
    def tool_schemas(self) -> list[dict]:
        return self._schemas

    def has(self, name: str) -> bool:
        return name in self._dispatch

    def call(self, full_name: str, args: dict) -> str:
        if self._loop is None or full_name not in self._dispatch:
            return f"未知 MCP 工具：{full_name}"
        session, tool_name = self._dispatch[full_name]
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._call(session, tool_name, args or {}), self._loop)
            return fut.result(timeout=90)
        except Exception as e:  # noqa: BLE001
            return f"调用 MCP 工具出错：{e}"

    async def _call(self, session, tool_name: str, args: dict) -> str:
        res = await session.call_tool(tool_name, args)
        parts = []
        for c in getattr(res, "content", []) or []:
            text = getattr(c, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts) or "（已执行，无文本输出）"
