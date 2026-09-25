# TravelAgent

一个基于 LangGraph 的全栈智能旅行规划项目。当前版本已经完成从“预留 Agent 接口”到“可运行的多 Agent 旅行规划流程”的第一轮升级，适合作为小组后续开发的共同基线。

- **前端**：React 18 + TypeScript + Vite + React Router
- **后端**：Python 3.13 + FastAPI + SQLAlchemy 2 + Pydantic v2
- **前端运行时**：Node.js 24.18.1 + npm 11
- **Agent**：LangGraph 多 Agent 工作流，支持需求理解、目的地探索、时间活动、餐饮、交通住宿和行程生成
- **数据**：默认使用 SQLite；会话状态已支持持久化，后续可切换 PostgreSQL/MySQL

## 当前版本做了什么

- 用户可以通过 `/api/agent/chat` 提交旅行需求并进行多轮交互。
- Agent 会先理解需求并生成候选目的地，等待用户选择后继续规划行程。
- 已支持对已生成方案提出局部修改，并返回受影响部分的重新规划结果。
- 工作流节点会产生结构化 trace，便于前端展示、调试和定位问题。
- Agent 会话状态默认保存到数据库，服务重启后不再完全依赖进程内存。
- 前端打开根路径后直接进入 Agent 对话页面，并提供行程列表和行程详情页面。

当前版本重点是稳定工作流和协作边界，不代表外部旅行数据能力和最终产品体验已经完成。

## 项目结构与文件职责

```text
Travel-Agent/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口、CORS、路由注册和建表
│   │   ├── config.py               # 环境变量和应用配置
│   │   ├── db.py                   # SQLAlchemy 数据库连接与会话
│   │   ├── api/routes/
│   │   │   ├── health.py           # 健康检查
│   │   │   ├── destinations.py     # 目的地 CRUD 接口
│   │   │   ├── trips.py            # 行程 CRUD 接口
│   │   │   └── agent.py            # Agent 对话、工作流和 trace 接口
│   │   ├── models/                 # 数据库 ORM 模型
│   │   │   ├── trip.py             # 行程数据
│   │   │   ├── destination.py      # 目的地数据
│   │   │   └── agent_session.py    # Agent 会话状态
│   │   ├── schemas/                # API 请求和响应的数据契约
│   │   ├── services/               # 非 Agent 业务逻辑和数据初始化
│   │   └── agent/
│   │       ├── agents/             # 领域 Agent：理解、目的地、时间、餐饮、交通、生成
│   │       ├── state.py            # LangGraph 共享状态和字段契约
│   │       ├── state_factory.py    # 新会话的初始状态
│   │       ├── graph.py            # 主流程和局部修改流程的图结构
│   │       ├── interaction.py      # 用户选择、确认和修订交互
│   │       ├── workflow_runner.py  # 执行工作流、合并状态和生成 trace
│   │       ├── session_store.py    # SQLite 持久化及内存测试存储
│   │       ├── specialists.py      # 专业任务的调度辅助
│   │       ├── revision.py         # 局部修改的影响和结果校验
│   │       ├── common.py           # LLM、解析和 Agent 通用工具
│   │       ├── travel_agent.py     # 面向 API 的 Agent 应用协调入口
│   │       └── nodes.py             # 旧导入路径兼容层，不新增业务逻辑
│   └── tests/
│       ├── test_health.py               # 服务健康检查
│       ├── test_agent_boundaries.py     # Agent 模块边界和基础契约
│       ├── test_dynamic_planning.py     # 动态规划流程
│       └── test_revision_validation.py  # 局部修改校验
├── frontend/
│   └── src/
│       ├── App.tsx                 # 前端路由入口
│       ├── main.tsx                # React 挂载入口
│       ├── components/             # Layout、TripCard 等通用组件
│       ├── pages/                  # Agent、行程列表和详情页
│       ├── api/client.ts           # 后端 API 调用
│       ├── api/types.ts            # 前端 API 类型和 trace DTO
│       ├── hooks/useApi.ts         # API 请求相关 Hook
│       └── styles/index.css        # 全局样式
├── CONTRIBUTING.md                 # 团队协作约定
└── docker-compose.yml              # 容器化启动配置
```

文件职责的基本依赖方向是：

```text
前端页面 → api/client.ts → api/routes → schemas / 应用协调入口
                                      → agent / services
                                      → models / 数据库 / 外部 API
```

新增 Agent 逻辑应放入对应的 `agent/agents/` 文件；`graph.py` 只负责组装节点和边；`travel_agent.py` 只负责协调，不要把专业 Agent 逻辑重新堆回入口文件。

## Agent 工作流

```text
用户需求
  → 需求理解
  → 目的地候选
  → 用户选择
  → 时间与活动规划
  → 餐饮推荐
  → 交通与住宿
  → 行程生成
```

用户修改已有方案时，流程会进入局部修改路径：分析影响范围，只重新生成受影响的内容，再返回新的方案和 trace。

## 快速开始

项目版本要求：Python `3.13`、Node.js `24.18.1`、npm `11`。Python 版本由 `.python-version` 固定，Node.js 版本由 `frontend/.nvmrc` 固定。

### 本地启动后端

```bash
cd backend

# Linux/macOS
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env

# Windows PowerShell：虚拟环境创建在项目根目录的 .venv 中
py -3.13 -m venv ..\.venv
..\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env

..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

后端地址：http://localhost:8000 ，接口文档：http://localhost:8000/docs 。如果使用 Windows PowerShell 的独立虚拟环境，请将上面的 Python 命令替换为 `.venv\Scripts\python.exe`。

首次启动后，可依次访问以下地址确认环境正常：

- http://localhost:8000/api/health：后端健康检查
- http://localhost:8000/docs：后端接口文档和请求模型
- http://localhost:5173：前端页面

### LLM 配置说明

复制 `backend/.env.example` 为 `backend/.env` 后，基础服务、数据库和默认测试不要求配置真实 LLM 密钥。要实际使用 Agent 对话，需要配置 `OPENAI_API_KEY`；使用 OpenAI 兼容服务时，还需要根据服务商配置 `OPENAI_API_BASE` 和 `OPENAI_MODEL`。真实 LLM 调用不属于默认 CI 验证范围，密钥不得提交到 Git。

### 本地启动前端

```bash
cd frontend
npm ci
npm run dev
```

前端地址：http://localhost:5173 。开发服务器会把 `/api` 请求代理到后端。

前端默认使用 `/api` 作为后端地址，开发环境由 Vite 代理到 `http://127.0.0.1:8000`，通常不需要额外配置。如果后端运行在其他地址，可在 `frontend/.env` 中设置 `VITE_API_BASE_URL`。

### Docker Compose

需要已安装并运行 Docker Desktop 或 Docker Engine。Compose 会等待后端 `/api/health` 健康检查通过后再启动前端：

```bash
docker compose up --build
```

服务地址：后端 http://localhost:8000 ，前端 http://localhost:5173 。停止服务：

```bash
docker compose down
```

默认开发数据库位于 `backend/travelagent.db`，仅用于本地开发，不应提交到 Git。

## 主要接口

以下是主要接口入口。请求参数、响应结构和可直接执行的示例以启动后的 `/docs` 以及 `backend/app/schemas/` 为准；本 README 不重复维护完整 API 契约。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET/POST | `/api/destinations` | 目的地列表 / 新增目的地 |
| GET/POST | `/api/trips` | 行程列表 / 新建行程 |
| GET/PATCH/DELETE | `/api/trips/{id}` | 查看 / 更新 / 删除行程 |
| POST | `/api/agent/chat` | Agent 多轮对话和规划 |
| GET | `/api/agent/workflow` | 获取工作流 Mermaid 图 |
| GET | `/api/agent/workflow/trace` | 获取工作流节点 trace |

## 配置

后端通过环境变量或 `backend/.env` 配置，参考 `backend/.env.example`：

- `DATABASE_URL`：数据库连接串，默认 `sqlite:///./travelagent.db`
- `CORS_ORIGINS`：允许跨域的前端来源
- `API_PREFIX`：接口前缀，默认 `/api`
- `OPENAI_API_KEY`：OpenAI API 密钥或其他兼容服务的密钥
- `OPENAI_API_BASE`：可选的 OpenAI 兼容接口地址
- `OPENAI_MODEL`：模型名称
- `OPENAI_TEMPERATURE`：模型温度
- `LLM_TIMEOUT_SECONDS`：LLM 请求超时时间
- `LLM_MAX_TOKENS`：LLM 最大输出 token 数

前端通过 `frontend/.env` 配置 `VITE_API_BASE_URL`，默认值为 `/api`，开发时由 Vite 代理到后端。

不要将 `.env`、API key、数据库文件或个人测试数据提交到仓库。

## 运行测试与质量检查

后端默认测试不调用外部 LLM，适合日常开发和 CI：

```bash
cd backend
python -m pip install -r requirements-dev.txt
python -m pytest
```

完整 Agent 集成测试和外部 API 验证尚未纳入默认测试套件。

提交前建议运行：

```bash
# 后端
cd backend
python -m ruff check app tests
python -m mypy
python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=75

# 前端
cd frontend
npm ci
npm run lint
npm run typecheck
npm run build
```

如果本次修改只涉及文档或 Docker 配置，也应运行：

```bash
git diff --check
```

GitHub Actions 会在 `main`/`master` 分支推送和 Pull Request 中运行后端和前端质量检查，配置位于 `.github/workflows/ci.yml`。

## 后续开发窗口

以下内容是当前需要继续深入开发的部分，组员可以按领域认领：

1. **外部旅行数据与事实验证**：接入目的地、天气、地图、交通、住宿和餐饮 API；增加来源记录、缓存、超时、限流和失败降级，避免 Agent 只依赖模型生成事实。
2. **Agent 规划质量**：完善真实 LLM 下的结构化输出、节点重试、错误恢复、成本控制和结果一致性验证。
3. **前端交互闭环**：完善候选目的地选择、规划阶段状态展示、trace 调试视图、局部修改确认和错误提示。
4. **行程数据模型**：继续明确行程卡片、每日活动、餐饮、交通和住宿的统一字段，减少后端状态与前端 DTO 的字段漂移。
5. **会话与部署**：补充会话过期和清理策略、数据库迁移、生产环境配置、鉴权以及 PostgreSQL 部署验证。
6. **测试与可观测性**：增加 API 契约测试、真实 LLM 的隔离集成测试、外部 API mock、日志和 trace 的统一记录。

当前阶段优先维护工程基础设施和既有功能稳定性。具体分支、提交、Pull Request 和配置安全约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 协作约定

- 修改 Agent 状态字段时，同时检查 `backend/app/agent/state.py`、相关 schema 和 `frontend/src/api/types.ts`。
- 修改工作流节点或边时，至少运行后端 pytest，并同步检查 `/api/agent/workflow` 和 trace 行为。
- 新增外部 API 时，先提供可替换的 service 接口和 mock，再接入真实供应商。
- 提交前避免提交 `.env`、数据库文件、构建产物和 `.pytest_cache`。

## 许可证

MIT
