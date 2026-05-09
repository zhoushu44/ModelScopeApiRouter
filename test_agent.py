import json
import httpx
import sys
from typing import Callable

BASE_URL = "http://localhost:2166/v1"
API_KEY = "multi-proxy-2025-2000q"
MODEL = "ZhipuAI/GLM-5"


def get_weather(city: str) -> str:
    mock_db = {"北京": "晴，25°C", "上海": "多云，28°C", "广州": "雷阵雨，30°C", "深圳": "阴，26°C"}
    return mock_db.get(city, f"未知城市 {city}")


def get_current_time() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def search_stock(symbol: str) -> str:
    mock = {"000001": "平安银行 12.34元 +1.2%", "600519": "贵州茅台 1688.00元 -0.5%"}
    return mock.get(symbol, f"未找到股票 {symbol}")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气情况",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称，如北京、上海"}
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "查询当前时间",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_stock",
            "description": "查询股票行情",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如000001、600519"}
                },
                "required": ["symbol"]
            }
        }
    }
]

TOOL_MAP: dict[str, Callable] = {
    "get_weather": get_weather,
    "get_current_time": get_current_time,
    "search_stock": search_stock,
}


def call_api(messages: list, tools: list | None = None) -> dict:
    payload = {"model": MODEL, "messages": messages}
    if tools:
        payload["tools"] = tools
    r = httpx.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def run_agent(question: str, tools: list | None = None) -> tuple[str, int]:
    """运行一次 Agent 交互，返回 (最终回答, 工具调用次数)"""
    messages = [{"role": "user", "content": question}]
    tool_call_count = 0

    for turn in range(3):  # 最多 3 轮工具调用
        result = call_api(messages, tools=tools)
        choice = result["choices"][0]
        message = choice["message"]

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return message.get("content", ""), tool_call_count

        messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            func = tc["function"]
            name = func["name"]
            args = json.loads(func["arguments"])
            print(f"  🔧 [{turn+1}] 调用工具: {name}({args})")

            fn = TOOL_MAP.get(name)
            output = fn(**args) if fn else f"错误: 未知工具 {name}"
            print(f"  📤 返回: {output}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": str(output),
            })
            tool_call_count += 1

    # 超过轮次限制，再请求一次让模型总结
    result = call_api(messages)
    return result["choices"][0]["message"].get("content", ""), tool_call_count


def test(name: str, question: str, expect_tools: int, tools: list | None = None):
    print(f"\n{'='*60}")
    print(f"🧪 测试: {name}")
    print(f"🧑 用户: {question}")
    try:
        answer, tc = run_agent(question, tools=tools)
        print(f"🤖 模型: {answer[:200]}{'...' if len(answer) > 200 else ''}")
        print(f"📊 工具调用次数: {tc}")
        if expect_tools >= 0 and tc != expect_tools:
            print(f"⚠️  警告: 期望工具调用 {expect_tools} 次，实际 {tc} 次")
            return False
        print("✅ 通过")
        return True
    except Exception as e:
        print(f"❌ 失败: {e}")
        return False


if __name__ == "__main__":
    results = []

    # 1. 多工具并行调用
    results.append(test(
        "多工具并行调用",
        "北京和上海的天气怎么样？现在几点了？",
        expect_tools=3,
        tools=TOOLS,
    ))

    # 2. 单工具调用
    results.append(test(
        "单工具调用",
        "深圳今天天气如何？",
        expect_tools=1,
        tools=TOOLS,
    ))

    # 3. 普通问答（不应触发工具）
    results.append(test(
        "普通问答无工具",
        "请用一句话解释什么是机器学习",
        expect_tools=0,
        tools=TOOLS,
    ))

    # 4. 需要推理后调用工具
    results.append(test(
        "推理后调用工具",
        "查询股票代码000001的行情，并告诉我现在的时间",
        expect_tools=2,
        tools=TOOLS,
    ))

    # 5. 工具返回未知结果，看模型如何处理
    results.append(test(
        "工具返回未知",
        "杭州的天气怎么样？",
        expect_tools=1,
        tools=TOOLS,
    ))

    # 6. 无 tools 参数的纯对话
    results.append(test(
        "无 tools 纯对话",
        "请讲一个简短的笑话",
        expect_tools=-1,  # 不校验次数
        tools=None,
    ))

    print(f"\n{'='*60}")
    passed = sum(results)
    total = len(results)
    print(f"📋 测试结果: {passed}/{total} 通过")
    if passed == total:
        print("🎉 全部通过！中转服务完全支持 Agent 工具调用场景。")
        sys.exit(0)
    else:
        print("⚠️  部分测试未通过，请检查模型对工具调用的支持情况。")
        sys.exit(1)
