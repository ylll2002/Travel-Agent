# Validate Agent 审核协议（第一阶段）

## 范围

统一审核输出、错误状态、问题定位与独立/主流程的上下文传递。实现位于 `shared/audit.py`，
由 ValidateAgent 和 Orchestrator 共同调用；没有模型 SDK 或第三方依赖。

规则真实性检查、修改后的再次审核、前端审核状态管理、安全与隐私功能不在本阶段实现。

## 输入

`validate_plan` 的参数为 `plan`、`profile`、`preferences`、`recent_trips`、`search`、`basic`。
调用方使用关键字参数，避免函数增加参数时产生错位。

- `plan`：实际方案、活动、路线、按方案费用、未知报价、可选 revision。
- `basic`：用户明确需求与硬约束。
- `search`：检索来源与天气证据。
- `profile/preferences/recent_trips`：画像、长期偏好和历史行程；不能替代用户明确要求。

## 输出示例

```json
{
  "schema_version": 1,
  "status": "blocked",
  "passed": false,
  "plan_revision": 3,
  "reviewed_styles": ["经典路线", "探索路线"],
  "issues": [{
    "severity": "high",
    "type": "时间",
    "detail": "活动结束后无法按现有交通耗时赶上列车。",
    "suggestion": "提前结束活动，保留进站时间。",
    "actionable": true,
    "source": "model",
    "plan_style": "经典路线",
    "day": 2,
    "date": "2026-10-11",
    "block_ids": ["b-activity", "b-train"],
    "evidence": [{"path": "plan.legs[2].duration_s", "value": 2700}]
  }],
  "feedback": "请调整活动结束时间。"
}
```

示例仅说明结构，实际定位字段必须对应输入中的真实活动与路线。
`passed/issues/feedback` 继续兼容原前端；新增字段由服务端生成或校验。

| status | 含义 | passed |
| --- | --- | --- |
| passed | 审核通过，没有问题 | true |
| warning | 审核通过，保留风险或优化建议 | true |
| blocked | 审核未通过 | false |
| error | 审核没有完成，包括初始化、超时或输出无效 | false |

`error` 状态包含安全的 `error` 文本和 `feedback`；不能视为发现了行程硬冲突。
`plan_revision` 来自待审方案；老方案没有合法非负整数 revision 时返回 null。
`reviewed_styles` 来自方案数据，不由模型声明。

## 问题字段

- `severity`：high、medium、low。high 表示有证据的执行冲突或违反用户明确硬约束。
- `type`：预算、交通、偏好、天气、时间、完整性、真实性。
- `detail/suggestion`：非空文本。
- `actionable`：明确的布尔值；缺失时默认 false，避免未经确认的自动修复。
- `source`：model 或 rule，由调用审核协议的可信代码指定，忽略模型自行填写的来源。
- `plan_style/day/date/block_ids`：问题位置；跨活动冲突允许多个 ID。不能确定时使用 null 和空列表，不能编造。
- `evidence`：包含 path、value 的列表。path 为输入字段路径，value 为对应值。

校验会拒绝无效严重程度、错误字段类型、不存在的活动 ID，以及方案/日期/天数与活动不匹配的定位。
老模型输出可不带 type、定位或证据；type 默认完整性，其余为空，不会根据描述猜测活动。
当前证据只校验结构，尚未验证 path/value 是否真实；不能据此声称实现了真实性检查。

模型返回 `passed=true` 却同时包含 high 问题时视为矛盾输出，再尝试一次；仍无效则返回 error。
旧模型若返回 `passed=false` 且只有 medium/low 问题，保留未通过结论，不静默改成通过，也不触发重跑。
下一阶段需在规则与模型整合中进一步统一判定策略。

## 反馈与历史

主流程和独立循环都只向 PlanAgent 发送 `high + actionable=true` 且有明确问题与建议的项。
服务错误和非严重优化不会触发重新规划；最多生成两轮。
历史保留 iteration、状态、方案版本、问题定位、证据、feedback 和错误信息（若有），便于检查每轮结果。

## 测试边界

离线测试覆盖协议校验、旧输出适配、定位一致性、错误状态、上下文参数传递和反馈次数。
它们验证程序行为，不代表真实模型审核质量、景点来源真实性或实时行程可执行性已经通过验收。
