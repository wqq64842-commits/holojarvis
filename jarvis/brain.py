"""贾维斯的大脑：OpenAI 兼容的中转站 + 工具调用 + 长期记忆 + MCP 工具。

通过你自己的中转站（OpenAI 兼容网关，/v1/chat/completions）调用任意大模型，
比如 DeepSeek、GPT、Claude 等——只要中转站支持「函数调用(tools)」。
负责把用户说的话理解成意图、必要时调用工具(本地工具/MCP工具)、处理多步任务，
最后给出一句简短的口语回复。

配置见 config.py：base_url.txt（中转站地址）/ api_key.txt（key）/ model.txt（模型名）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config, memory, tools

SYSTEM_PROMPT = """你是「贾维斯」(Jarvis)，用户电脑上的中文语音助手，风格像电影里钢铁侠的 AI 管家：\
干练、礼貌、略带幽默。

重要规则：
1. 你的回复会被语音朗读出来，所以必须简短、口语化，一般一两句话即可，不要列清单、不要用 markdown、不要念网址。
2. 用户的话来自语音识别，可能有错别字或同音字，请结合上下文理解真实意图。
3. 能用工具完成的事就调用工具，别只是空谈。
4. 不确定时可以追问一句，但尽量主动把事办了。
5. 始终用简体中文回答。
6. 发微信(send_wechat)前，必须先口头复述"要发给谁、内容是什么"并等用户确认后，才在下一轮真正调用工具发送。
7. 用户问屏幕上的内容、让你总结当前页面/文章时，调用 read_screen 看屏幕再回答。
8. 长期记忆：当用户透露自己的名字、偏好、习惯、常用设置等值得长期记住的信息时，主动调用 remember 记下来；下面"已经记住的事"要自然运用。
9. 多步任务：遇到"整理文件夹""批量重命名"等需要好几步的活儿，先用 list_directory 看现状，再分步用 run_shell / move_to_trash 执行；删除一律用 move_to_trash；批量或有风险的操作执行前先口头确认。办完用一句话汇报结果。"""


def _os_hint() -> str:
    """告诉大脑当前操作系统，好让 run_shell 用对命令语法。"""
    shell = "PowerShell" if config.IS_WINDOWS else "bash/zsh"
    return (f"\n\n[运行环境] 你现在运行在 {config.OS_NAME} 上；"
            f"run_shell 执行的是 {shell} 命令，请按该系统的命令语法来写命令。")


def _to_openai_tool(t: dict) -> dict:
    """把 Anthropic 风格的工具 schema 转成 OpenAI 的 function 格式。"""
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {
                "type": "object", "properties": {}},
        },
    }


class Brain:
    def __init__(self, api_key: str, mcp=None) -> None:
        self._api_key = api_key
        self._mcp = mcp
        self._messages: list[dict] = []
        # 本地工具 + MCP 工具，统一转成 OpenAI function 格式
        anthropic_tools = list(tools.TOOL_SCHEMAS)
        if mcp:
            anthropic_tools += mcp.tool_schemas()
        self._tools = [_to_openai_tool(t) for t in anthropic_tools]
        # 把运行环境(OS) + 长期记忆拼进系统提示
        self._system = SYSTEM_PROMPT + _os_hint() + memory.as_prompt()

    def reset(self) -> None:
        self._messages = []

    def _dispatch(self, name: str, args: dict) -> str:
        if self._mcp and name.startswith("mcp__"):
            out = self._mcp.call(name, args)
        else:
            out = tools.run(name, args)
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)

    def _chat(self, messages: list[dict]) -> dict:
        """调一次中转站 /chat/completions，返回 choices[0].message。"""
        body = {
            "model": config.MODEL,
            "messages": [{"role": "system", "content": self._system}] + messages,
            "tools": self._tools,
            "tool_choice": "auto",
            "max_tokens": config.MAX_TOKENS,
            "stream": False,
        }
        req = urllib.request.Request(
            config.llm_endpoint(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"中转站返回 {e.code}：{detail}") from None
        return data["choices"][0]["message"]

    def ask(self, user_text: str) -> str:
        """处理一句用户输入，返回要朗读的回复文本。"""
        self._messages.append({"role": "user", "content": user_text})

        # 工具调用可能来回多轮（多步任务），循环直到模型不再调用工具
        for _ in range(8):
            msg = self._chat(self._messages)
            tool_calls = msg.get("tool_calls") or []
            # 原样保存这条 assistant 消息（含 tool_calls，供下一轮上下文）
            assistant: dict = {"role": "assistant",
                               "content": msg.get("content") or ""}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            self._messages.append(assistant)

            if not tool_calls:
                return (msg.get("content") or "").strip()

            for tc in tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                output = self._dispatch(fn.get("name", ""), args)
                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": output,
                })

        return "抱歉，这个有点复杂，我先停一下。"
