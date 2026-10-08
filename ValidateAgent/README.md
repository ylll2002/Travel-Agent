# ValidateAgent - 旅行方案审核 Agent

审核 PlanAgent 方案的预算、交通、偏好、天气、时间和完整性，并提供定位明确的修改建议。
目前使用模型审核；规则检查将在下一阶段补充。

## 安装

```bash
cd ValidateAgent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

在 `.env` 中填写 OpenAI 兼容接口的 Key、地址和模型；不要提交真实密钥。

## 单独审核

```bash
cat validate_input.json | .venv/bin/python validate.py
```

输入字段：`plan`、`profile`、`preferences`、`recent_trips`、`search`、`basic`。
其中 `basic` 保存用户明确需求（预算、人数、日期等），`search` 保存来源信息。
CLI 和内部函数均使用同一审核协议，见 [审核协议](../docs/VALIDATE_AGENT.md)。

## 独立审核循环

```bash
cat loop_input.json | .venv/bin/python run.py
```

输入为 PlanAgent 的规划上下文，包含上述画像、需求和搜索信息。
需先配置 `PlanAgent/.venv`。循环最多生成两轮（首次生成，至多修正一次）：

- 审核通过，含非严重建议：直接返回。
- 未通过且存在 `high + actionable=true` 的问题：携带这些严重问题反馈，修正一次。
- 服务错误、无可修复严重问题：保留方案和审核状态，不重新生成。
- 第二轮仍未通过：返回实际未通过的审核结论，不伪装为成功。

输出包含 `passed`、`plan`、`audit`、`history`、`final_feedback`。
网站主流程由 Orchestrator 驱动；本入口用于独立调试，复用同一协议和反馈筛选。

## 离线测试

在仓库根目录执行，不调用真实模型：

```bash
ValidateAgent/.venv/bin/python -m unittest discover -s ValidateAgent -p 'test_*.py' -v
Orchestrator/.venv/bin/python -m unittest discover -s Orchestrator -p 'test_audit*.py' -v
```
