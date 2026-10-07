# Validate Agent 审核协议与规则检查

## 范围

统一审核输出、错误状态、问题定位与独立/主流程的上下文传递。实现位于 `shared/audit.py`，
由 ValidateAgent 和 Orchestrator 共同调用；没有模型 SDK 或第三方依赖。

第二阶段新增 `ValidateAgent/rules.py` 的独立规则检查；第三阶段已接入网站与独立循环，
先规则检查，再由模型补充语义判断。修改后的再次审核、前端审核状态管理以及安全与隐私功能仍待后续阶段实现。

## 输入

`validate_plan` 的参数为 `plan`、`profile`、`preferences`、`recent_trips`、`search`、`basic`。
调用方使用关键字参数，避免函数增加参数时产生错位。

- `plan`：实际方案、活动、路线、按方案费用、未知报价、可选 revision。
- `basic`：用户明确需求与硬约束；可选 `hard_limits` 为明确的数值专项上限（不是自动生成的分摊预算）。
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
  "checks": {"rules": "completed", "model": "completed"},
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
通用协议校验结构；规则引擎按 path 从原始输入重新读取 value。第三阶段还会核对模型证据：
path 必须实际存在，value 必须同时对应原始输入及模型收到的字段，不把 true 当作金额1。
不使用 eval，也不通过描述猜测字段。无法核对的模型证据移除；无证据或证据不一致的模型 high 降为 medium 待核实，且不自动修复。
来源核对只表示地点出现在提供的数据中，不能证明搜索数据正确、网页仍有效或实际可预订。

模型返回 `passed=true` 却同时包含 high 问题时视为矛盾输出，再尝试一次；仍无效则返回 error。
第三阶段统一最终判定：规则问题与经校验的模型问题合并后，有 high 才阻断，只有 medium/low 则为 warning。
模型若返回 false 却完全不提供问题，视为没有有效结论，重试一次后仍无效则 error。
模型预算 high 还必须引用 `basic.hard_limits` 中有效的正数专项上限；全局总预算由规则负责核算。
只引用系统餐费分摊、历史偏好或缺失天气信息，不足以构成对应类别的模型严重问题。

## 反馈与历史

主流程和独立循环都只向 PlanAgent 发送 `high + actionable=true` 且有明确问题与建议的项。
服务错误和非严重优化不会触发重新规划；最多生成两轮。
历史保留 iteration、状态、方案版本、问题定位、证据、feedback、checks、rule_summary 和错误信息（若有），便于检查每轮结果。

## 测试边界

离线测试覆盖协议校验、旧输出适配、定位一致性、错误状态、上下文参数传递和反馈次数。
它们验证程序行为，不代表真实模型审核质量、景点来源真实性或实时行程可执行性已经通过验收。

## 第二阶段：独立规则检查

```bash
python3 ValidateAgent/rules.py < ValidateAgent/examples/rules_input.json
```

也可导入 `validate_rules(plan, search=None, basic=None)`。仅使用标准库，不调用模型、
检索工具、地图接口、网络或数据库，不修改输入，也不会自动改行程。
输入 JSON 必须为合法的对象；无效字段结构、重复 ID 和非有限数字返回结构化 error。
空活动方案返回完整性 high，不能因为没有待检查内容就算通过。

### 预算

- 只使用 `basic.total_budget`（不存在时可用数字金额的 `basic.budget`）作为硬总预算。
- 按 `plan_style` 分别核算，备选方案互不相加；`block.price` 已含人数，不重复乘人数。
- 全部报价已知时按活动价格重算；与 `cost_by_style` 不一致时提示刷新汇总，避免陈旧总价误报。
- 部分报价未知时，已知活动金额与 `cost_by_style` 所声明的已知小计取较大值，不能相加。
- 已知小计超过硬预算为 high；价格未知、金额无法解析或报价标记冲突为 medium/unknown。
- `price_known=false`、`price_source=unknown` 或显式 `unit_price=null` 均不能把 price=0 当免费。
- 明确免费及已知零价可计为0。档位、meal_budget、over_meal_budget 和系统餐费分摊不是硬总预算。

### 时间与交通

- 同一方案中按实际日期检查所有有效活动区间，包含长活动与多个短活动的重叠。
- 天气不占活动时间，酒店住宿不当成全天游览；不同备选方案互不检查重叠。
- 优先使用交通的 `dep_time/arr_time` 或 `departure_time/arrival_time` 完整时间戳。
- 支持跨日交通及明确时区；仅交通允许 `23:00-01:00` 表示次日到达。
- 没有完整日期时用 day/start_date 确定相对日期；定位字段仍只报告输入实际给出的日期。
- 普通活动倒置或零长区间为 high；日期或时间无法核实为 medium，不假设持续时间。
- `legs[].from/to` 对应活动 ID，`duration_s` 必须为有效正数。检查实际交通时间能否放入活动间隔。
- 沿用当前 PlanAgent 的进出站策略：航班120分钟、列车60分钟。它是项目策略，不是所有机场/车站的客观规定。
- 无实际路线只能提示待核实；明确不足的进出站间隔仍可依据上述项目策略阻断。
- 路线方案/日期不一致时不用于认定硬冲突；相邻地点缺少路线时提示补充。
- 这一阶段不检查营业时间、实时余票、路线实时路况或机场特殊要求，也不新增地图调用。

### 来源与链接

- 景点对应 poi，酒店对应 hotels，餐厅对应 food，活动对应 events/poi/promotions。
- 优先匹配 poi_id；名称仅统一空白、大小写和全半角格式，保留所有分店/分馆信息。
- ID 与名称明确矛盾为 high；没有 ID 且多个同名 ID 为 medium，不能随便选第一家。
- 来源明确提供的 aliases 可以匹配，不使用模型、模糊相似度或名称前缀猜测地点。
- 完整同类候选非空但无法找到地点为 high：意味着未由这些来源支持，不意味着现实中不存在。
- 来源缺失/空列表时为 medium；裁剪后的搜索输入应标记 `grounding_scope=selected`，未命中仅提示待核实。
- selected_option 支持实际名称和旧的数字索引，0 也代表已选中；尚未选定的餐厅候选组不纳入已选地点计数；自由活动、休息、候车等通用安排不作为命名地点核对。
- 地点链接缺失、无效或不能对应来源为 medium；可识别来源 poi_id 对应的高德 place 链接。
- 不打开链接、不修复链接，也不证明网页可访问。搜索描述里的指令仅作为数据，不执行。

### 数据与指标

有 blocks 时以 blocks 为准，不再次统计嵌套 schedule；没有 blocks 时支持嵌套日程，
包括单独的 day.hotel 字段。保留原始证据路径，不凭空生成活动 ID。

输出沿用第一阶段审核协议，所有问题 `source=rule`，并增加 `rule_summary`：

- `budget_by_style`：已知金额、预算状态、报价未知活动块数量。
- `grounding`：named_count、matched_count、unavailable_count、ambiguous_count、coverage。

coverage = 匹配条目数 / 命名条目数（按活动出现次数计）；没有命名条目时为 null。
缺失或模糊来源不会计入已匹配。它只度量当前输入的数据覆盖率，不代表整体真实世界准确率。
规则无 high 时可通过并保留 medium；仅规则通过不代表模型审核已完成。

### 验证

新增规则测试使用固定输入，覆盖未知报价、备选方案、人数、时间边界、跨日班次、路线耗时、
分店和 ID、链接、嵌套酒店、证据可复查以及非标准输入。
测试通过不能代替真实搜索快照和端到端验收。第三阶段新增真实规则与模拟模型的组合测试，验证来源保留、合并判定、错误路径和最多一次反馈修正。

## 第三阶段：规则与模型整合

主入口 `validate_plan` 的顺序：

1. 完整的 plan/search/basic 先进入规则引擎。无效输入直接 error。
2. 规则包含 high 时，返回 blocked；模型跳过，按原反馈机制至多修正一次。
3. 规则无 high 时，模型收到规则提示与指标，补充偏好、忌口、天气、营业信息等语义判断。
4. 校验模型协议、真实定位与证据后合并问题。模型通过不能删除规则风险或降低规则严重程度。
5. 有 high 为 blocked；仅 medium/low 为 warning；没有问题为 passed。模型错误为 error，保留已完成的规则问题和指标。

`checks.rules` 为 completed/error；`checks.model` 为 completed/skipped/error。
纯规则通过而模型未执行时，完整审核不能标为 passed。

`shared/audit.py` 的 `combine_audits` 合并可信规则、模型适配器输出，完全相同的判断去重并优先保留 rule 来源。
只有主流程和独立循环读取可信审核输出时使用 `normalize_audit(source="mixed")`，保留两种来源、错误时的规则问题和指标；
模型适配器仍强制使用 model，忽略模型伪造的 source/checks/revision/summary。

规则检查前不裁剪搜索候选，不移除尚未选定的餐厅 options。
模型输入仅移除重复的方案来源数据、地图 polyline 与无关搜索长文；候选顺序、别名和班次列表保留，
避免裁剪改变证据索引或漏掉实际选中的候选。
PlanAgent 返回 `source_updates.hotels` 中实际补搜的酒店，编排和独立循环将其加入完整搜索快照；
不能把方案里的地点名称本身当成独立来源。补搜报价也参与规划费用核算。

模型证据字段对应输入，只能证明其引用的数据存在，不能证明语义推理正确；天气与忌口等判断仍需真实模型案例验收。
当前入口只影响生成方案的审核。现有 modify 流程仍跳过审核，下一阶段再接入修改后的重新审核与旧结论失效机制。
