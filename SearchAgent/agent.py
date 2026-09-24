"""SearchAgent 的 agent（LangGraph + MCP）。

用 LangGraph 的 create_react_agent 构建 ReAct agent，
通过 langchain-mcp-adapters 把 tools.py 里的 MCP 工具桥接为 LangChain 工具，
LLM 使用 DeepSeek（OpenAI 兼容接口，模型 deepseek-v4-pro）。

运行前请在 ``.env`` 中配置（参考 ``.env.example``）。
"""

import asyncio
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver

TA_DIR = Path(__file__).resolve().parent
TOOLS_PATH = TA_DIR / "tools.py"

load_dotenv(TA_DIR / ".env")

SYSTEM_PROMPT = (
    f"今天是 {date.today().strftime('%Y-%m-%d')}。"
    "你是一个数据检索 agent，职责是根据需求调用工具查询数据，"
    "并把检索结果返回给其他 agent 做后续处理和最终回答，"
    "因此你不要总结、不要推荐、不要面向最终用户润色。"
    "可用工具及对应意图：get_weather=天气；search_hotels=酒店/住宿/住哪里；"
    "search_flights=机票/航班/怎么去；search_poi=景点/风景名胜/玩什么；"
    "search_events=演唱会/比赛/节日/活动；search_food=美食/餐厅/小吃/吃什么。"
    "用户提问往往隐含检索意图，请主动判断意图并调用对应工具"
    "（例如“杭州有什么好吃的”→search_food），再把工具结果结构化输出。"
    "用户提到的日期如果没有年份，默认按今年处理。"
    "输出时把工具返回的结果结构化、原样返回（可用 JSON，results 放原始条目）；"
    "没有匹配工具或未查到数据时返回 {\"results\": []}。"
)


def _build_model() -> ChatOpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未检测到 OPENAI_API_KEY。请复制 .env.example 为 .env，"
            "并填入你的 API Key（以及可选的 OPENAI_BASE_URL、OPENAI_MODEL）。"
        )
    kwargs: dict[str, Any] = {
        "model": os.getenv("OPENAI_MODEL", "deepseek-v4-pro"),
        "api_key": api_key,
        "temperature": 0,
    }
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    return ChatOpenAI(**kwargs)


def _content_to_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


async def _reply(agent: Any, config: dict, query: str) -> str:
    result = await agent.ainvoke({"messages": [("user", query)]}, config=config)
    last = result["messages"][-1]
    return _content_to_str(last.content)


async def run() -> None:
    model = _build_model()
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "main"}}

    server_config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(TOOLS_PATH)],
        "cwd": str(TA_DIR),
    }

    client = MultiServerMCPClient({"search-tools": server_config})
    tools = await client.get_tools()
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )

    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        print(await _reply(agent, config, query))
        return

    print("输入 exit / quit 退出。")
    while True:
        query = input("你想了解什么？> ").strip()
        if not query:
            continue
        if query.lower() in {"exit", "quit", "q"}:
            break
        print(await _reply(agent, config, query))


if __name__ == "__main__":
    asyncio.run(run())
