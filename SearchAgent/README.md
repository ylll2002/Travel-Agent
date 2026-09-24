# SearchAgent - 数据检索 agent 模块

一个「LangGraph agent + MCP 工具」示例，内置六个工具：

- `get_weather`：查询某日期/日期段的逐日天气（Open-Meteo，无需 key）
- `search_hotels`：搜索目的地酒店（飞猪 FlyAI）
- `search_flights`：搜索机票（飞猪 FlyAI）
- `search_poi`：搜索景点/风景名胜（飞猪 FlyAI）
- `search_events`：按地点和时间搜索热点活动（演唱会/比赛/节日，DuckDuckGo 网络搜索）
- `search_food`：搜索美食/餐厅（大众点评/抖音/小红书，DuckDuckGo 网络搜索）

文件说明：

- `tools.py`：MCP **服务端**，暴露上述工具
- `agent.py`：LangGraph **ReAct agent**，通过 `langchain-mcp-adapters` 把 MCP 工具桥接成
  LangChain 工具，用 DeepSeek（`deepseek-v4-pro`）做数据检索，并把结果返回给其他 agent 处理

## 安装

```bash
cd SearchAgent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install          # 安装 flyai CLI（酒店/机票搜索，依赖 Node）
```

## 配置

```bash
cp .env.example .env
```

然后编辑 `.env`，填入你的 `OPENAI_API_KEY`（默认走 DeepSeek，`OPENAI_BASE_URL` 和
`OPENAI_MODEL` 可改，兼容任何 OpenAI 风格接口）。

酒店/机票工具默认即可**免 key 试用**（价格会被模糊显示）。如需更完整的数据和更稳定的
调用，去飞猪控制台获取 API Key 后配置：

```bash
./node_modules/.bin/flyai config set FLYAI_API_KEY "你的key"
```

## 运行

```bash
python agent.py "帮我查一下杭州的酒店"
python agent.py "北京到上海10月1日的机票"
python agent.py "东京现在天气怎么样？"
```

不带参数则进入交互模式（多轮对话，带记忆）。agent 会启动 `tools.py` 作为 MCP 子进程，
由 LangGraph 的 ReAct 循环决定调用合适的工具并返回检索结果。

## 已知限制与建议

`search_events` 和 `search_food` 底层用的是免费的 DuckDuckGo 网络搜索（`ddgs`），
无需 API Key，但有两点局限：

- 高频调用时会被限流，可能间歇性返回空结果或结果偏少（`tools.py` 里已加「重试 + 指数退避」缓解）。
- 结果偏中文 SEO 站点，覆盖和权威性一般，且不一定能稳定凑满 50 条。

如果追求稳定和更好的结果质量，建议换成付费搜索 API（三者选一即可）：

- Tavily：对 AI agent 最友好，支持日期范围过滤
- Serper：Google 搜索结果
- Bing Search：微软官方 API

接入后只需把 `tools.py` 里的 `_web_search` 替换成对应 API 的调用，工具签名和 agent 侧无需改动。

## 单独测试工具服务

`tools.py` 本身就是一个标准 MCP server（stdio），可以用任何 MCP 客户端连接，例如在
Claude Desktop / Cursor 等工具里配置：

```json
{
  "mcpServers": {
    "search-tools": {
      "command": "/绝对路径/SearchAgent/.venv/bin/python",
      "args": ["/绝对路径/SearchAgent/tools.py"]
    }
  }
}
```
