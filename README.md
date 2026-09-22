# TravelAgent

一个旅行规划 Agent 的全栈项目框架：

- **前端**：React 18 + TypeScript + Vite + React Router
- **后端**：Python 3.13 + FastAPI + SQLAlchemy 2 + Pydantic v2
- **数据**：默认使用 SQLite（开发环境），可无缝切换为 PostgreSQL/MySQL
- **Agent**：预留了 `/api/agent/chat` 接口与 `TravelAgent` 服务类，方便后续接入 LLM

## 目录结构

```text
TravelAgent/
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── main.py          # 应用入口（CORS、路由注册、建表）
│   │   ├── config.py        # 配置（pydantic-settings）
│   │   ├── db.py            # SQLAlchemy engine / session
│   │   ├── models/          # ORM 模型
│   │   ├── schemas/         # Pydantic 请求/响应模型
│   │   ├── api/routes/      # 路由层（health / trips / destinations / agent）
│   │   ├── services/        # 业务逻辑与数据初始化
│   │   └── agent/           # AI 助手占位实现
│   └── tests/               # pytest 测试
├── frontend/                # React 前端
│   └── src/
│       ├── api/             # API 客户端与类型定义
│       ├── components/      # 通用组件
│       ├── pages/           # 页面
│       ├── hooks/           # 自定义 hooks
│       └── styles/          # 全局样式
└── docker-compose.yml       # 可选的一键容器化启动
```

## 快速开始

### 1. 启动后端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

后端默认运行在 http://localhost:8000 ，接口文档见 http://localhost:8000/docs 。

### 2. 启动前端

```bash
cd frontend
npm install
npm run dev
```

前端默认运行在 http://localhost:5173 ，开发服务器会把 `/api` 请求代理到后端。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET/POST | `/api/destinations` | 目的地列表 / 新增目的地 |
| GET/POST | `/api/trips` | 行程列表 / 新建行程 |
| GET/PATCH/DELETE | `/api/trips/{id}` | 查看 / 更新 / 删除行程 |
| POST | `/api/agent/chat` | AI 旅行助手对话 |

## 配置

后端通过环境变量或 `backend/.env` 配置（参考 `backend/.env.example`）：

- `DATABASE_URL`：数据库连接串，默认 `sqlite:///./travelagent.db`
- `CORS_ORIGINS`：允许跨域的前端来源
- `API_PREFIX`：接口前缀，默认 `/api`

前端通过 `frontend/.env` 配置 `VITE_API_BASE_URL`（默认 `/api`，走开发代理）。

## 运行测试

```bash
cd backend
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## 接入 LLM

编辑 `backend/app/agent/travel_agent.py`，在 `TravelAgent.chat()` 中接入你选择的模型
（如 OpenAI、通义、DeepSeek 等），前端聊天页面无需改动即可生效。

