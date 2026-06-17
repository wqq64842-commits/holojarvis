"""中转站连通性自检：跑 `./.venv/bin/python test_llm.py`
依次测试 ① 基础对话 ② 工具调用，能过就说明贾维斯的大脑接好了。"""
import sys
sys.path.insert(0, ".")
from jarvis import config, brain  # noqa: E402

print(f"模型 : {config.MODEL}")
print(f"地址 : {config.LLM_BASE_URL}  ->  {config.llm_endpoint()}")
key = config.load_api_key()
print(f"Key  : {'已读到（' + key[:6] + '…）' if key else '✗ 没读到'}")
if not config.LLM_BASE_URL or not key:
    print("\n请先填 base_url.txt 和 api_key.txt。")
    sys.exit(1)

b = brain.Brain(api_key=key, mcp=None)

print("\n① 基础对话测试……")
try:
    print("   贾维斯：", b.ask("用一句话介绍你自己"))
except Exception as e:  # noqa: BLE001
    print("   ✗ 失败：", e)
    sys.exit(1)

print("\n② 工具调用测试（让它报时间，应触发 get_time 工具）……")
b.reset()
try:
    print("   贾维斯：", b.ask("现在几点了？"))
    used = any(m.get("role") == "tool" for m in b._messages)
    print("   工具是否被调用：", "✅ 是" if used else "⚠ 否（模型没调工具，但对话通了）")
except Exception as e:  # noqa: BLE001
    print("   ✗ 失败：", e)
    sys.exit(1)

print("\n✅ 中转站接好了。现在可以 ./run.sh 启动贾维斯。")
