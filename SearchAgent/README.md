# SearchAgent - 数据检索 agent 模块

一个「LangGraph agent + MCP 工具」示例，内置五个工具：

- `get_weather`：查询某日期/日期段的逐日天气（Open-Meteo，无需 key）
- `search_hotels`：搜索目的地酒店（飞猪 FlyAI）
- `search_flights`：搜索机票（飞猪 FlyAI）
- `search_poi`：搜索景点/风景名胜（飞猪 FlyAI）
- `search_promotions`：检索飞猪促销活动/优惠商品（飞猪 FlyAI）

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

## JSON / 自然语言统一入口

```bash
# JSON 输入
echo '{"destination":"宁波","start_date":"2026-10-01","end_date":"2026-10-03"}' | python search.py

# 自然语言输入（会先用 DeepSeek 解析成 JSON）
python search.py "宁波 10月1日到10月5日"
```

`search.py` 统一入口：输入是 JSON 则直接调用 `run_search`，是自然语言则先解析成 JSON；
输出为结构化 JSON，包含 `weather`、`hotels`、`poi`、`promotions` 四个字段（并行调用）。

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
