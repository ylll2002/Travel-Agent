# ValidateAgent - 旅行方案审核 Agent

审核 PlanAgent 方案的预算、交通、偏好、天气、时间和完整性，并提供定位明确的修改建议。
网站与独立审核入口现在都先执行规则检查，再由模型补充语义判断。规则发现的严重冲突不能被模型覆盖。

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

## 无模型的规则检查

在仓库根目录运行，无需模型 Key、网络或第三方 Python 库：

```bash
python3 ValidateAgent/rules.py < ValidateAgent/examples/rules_input.json
```

示例是合成数据，会发现超预算、交通间隔不足和地点未出现在候选列表中的问题。
也可将真实方案、完整搜索快照和明确需求保存为同结构 JSON，再输入该入口。
输入为 `{"plan": {...}, "search": {...}, "basic": {...}}`。

这个入口仅执行规则，仍无需模型配置；`validate.py` 则执行第三阶段的规则与模型整合流程。
规则发现严重问题时先返回修正，不调用模型；修正后的新方案再次审核，规则不阻断才调用模型。
详情见 [审核协议和规则](../docs/VALIDATE_AGENT.md)。

## 独立审核循环

```bash
cat loop_input.json | .venv/bin/python run.py
```

输入为 PlanAgent 的规划上下文，包含上述画像、需求和搜索信息。
需先配置 `PlanAgent/.venv`。规划时补搜的餐厅与酒店来源会传给审核。
循环最多生成两轮（首次生成，至多修正一次）：

- 审核通过，含非严重建议：直接返回。
- 首次生成未通过且存在 `high + actionable=true` 的问题：携带这些严重问题反馈，修正一次。
- 输入含 modify 时：修改后审核一次并返回结果，避免自动重新执行修改指令。
- 服务错误、无可修复严重问题：保留方案和审核状态，不重新生成。
- 第二轮仍未通过：返回实际未通过的审核结论，不伪装为成功。

输出包含 `passed`、`plan`、`audit`、`history`、`final_feedback`。
`audit.checks` 记录规则和模型完成、跳过或错误的状态；`rule_summary` 保留预算和来源覆盖指标。
模型出错时返回 error 并保留规则问题，不把仅规则通过显示为完整通过。
网站主流程由 Orchestrator 驱动；本入口用于独立调试，复用同一协议和反馈筛选。

## 离线测试

在仓库根目录执行，不调用真实模型：

```bash
ValidateAgent/.venv/bin/python -m unittest discover -s ValidateAgent -p 'test_*.py' -v
Orchestrator/.venv/bin/python -m unittest discover -s Orchestrator -p 'test_audit*.py' -v
```

## 本地查看整合结果

启动网站所需的后端和前端后，重新生成一份方案即可进入新的审核流程。
在浏览器开发者工具的 Network 中查看 `/api/plan` 返回数据，
或 `/api/plan/stream` 最后一条 `type=final` 事件的 `data`：

- `audit.checks.rules=completed` 表示规则执行完成。
- `audit.checks.model=skipped` 且 status=blocked 表示规则先发现严重冲突，模型未调用。
- `model=completed` 表示模型补充审核也已完成；medium/low 提示仍保留。
- `status=error` 表示审核未完成，不能当作通过，也不会作为行程冲突自动重跑。
- `history` 记录至多两轮的不同结论，问题的 `source` 区分 rule 和 model。

网站修改已有方案后已在路线和费用更新完成时重新审核，新结论绑定递增的 revision。
审核失败会保留修改并标记 error，旧版本的通过结论失效。修改不会因审核建议再次自动重写。
网页现有展示不会呈现全部新增指标；详细问题定位和页面状态提示属于第五阶段。
离线测试均不调用真实模型；真实模型的证据输出质量仍需用实际搜索快照验收。

第四阶段还会保留原有明确需求、长期偏好和历史行程上下文。
修改接口维持扁平方案响应；生成接口仍返回 plan/audit/history 的原有包装结构。
前端自动验证可在 frontend 运行 `npm run test:review`，完整网站手动验收可留到第五阶段完成后。
