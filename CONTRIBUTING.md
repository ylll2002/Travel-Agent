# Contributing to TravelAgent

感谢参与 TravelAgent 开发。当前项目处于工程基础整理阶段，提交应优先保持结构清晰、环境可复现，并避免把业务实验和基础设施改动混在同一个变更中。

## 开发环境

- Python：`3.13`，版本由根目录 `.python-version` 固定
- Node.js：`24.18.1`，版本由 `frontend/.nvmrc` 固定
- npm：`11`
- 后端依赖：`backend/requirements-dev.txt`
- 前端依赖：`frontend/package-lock.json`

敏感配置放在本地 `.env` 文件中。不要提交 API key、数据库文件、依赖目录、构建产物或测试缓存。

## 分支

- `main`：稳定主分支
- `feature/<short-name>`：新功能
- `fix/<short-name>`：缺陷修复
- `docs/<short-name>`：文档或项目治理
- `chore/<short-name>`：依赖、CI 或工具配置

请从最新的 `main` 创建分支，并保持一个分支聚焦一个主题。

## 提交前检查

后端：

```bash
cd backend
python -m ruff check app tests
python -m mypy
python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=75
```

前端：

```bash
cd frontend
npm ci
npm run lint
npm run typecheck
npm run build
```

如果只修改文档或 Docker 配置，也请运行：

```bash
git diff --check
```

## Pull Request

Pull Request 描述至少应包含：

- 变更目的和影响范围
- 关键实现或配置变化
- 已执行的验证命令及结果
- 是否涉及数据库、环境变量或部署行为变化
- 尚未解决的限制或后续工作

提交前请确认：

- 没有提交 `.env`、密钥、数据库文件或构建产物
- 新增行为有对应测试，或在 PR 中说明为什么不需要测试
- API、环境变量、启动方式变化已同步更新 README
- 没有把与当前任务无关的重构混入变更

## 代码边界

当前阶段优先维护工程基础设施和既有功能稳定性。Agent 内部能力、外部旅行 API 和 API 契约扩展属于后续阶段，除非任务明确要求，否则不要在基础设施变更中顺带修改。

后端按职责组织：

- `backend/app/api/routes/`：HTTP 路由
- `backend/app/schemas/`：请求和响应模型
- `backend/app/services/`：业务服务
- `backend/app/agent/`：Agent 工作流实现
- `backend/tests/`：后端测试
- `frontend/src/`：前端页面、组件和 API 客户端

## 配置与数据

- 使用 `backend/.env.example` 和 `frontend/.env.example` 作为配置参考。
- 默认 SQLite 数据库只用于本地开发。
- 真实 LLM 调用不属于默认测试和 CI；需要单独配置密钥并明确标注验证范围。
- 不要将个人测试数据或本地数据库提交到仓库。
