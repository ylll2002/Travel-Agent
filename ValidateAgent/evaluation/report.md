# Validate Agent 离线回归评估

本报告由固定合成案例自动生成。默认运行不调用模型、不联网。

**这不是实际模型准确率报告。hybrid_fixture 使用预置模型输出，仅验证合并和证据处理。**

数据集 SHA256：`ae374f5360b7b9b6553919e87a1d3c411a954cb59730aaaf7df67af2662c62b3`

| 指标 | 结果 |
|---|---:|
| 案例数 | 25 |
| 符合全部预期的案例 | 25 |
| 状态匹配比例 | 1.0 |
| 严重问题正确命中 | 11 |
| 严重问题误报 | 0 |
| 严重问题漏报 | 0 |
| 严重问题精确率（此案例集） | 1.0 |
| 严重问题召回率（此案例集） | 1.0 |
| 错误阻断案例 | 0 |
| 应阻断却未阻断案例 | 0 |
| 问题依据条数 | 30 |
| 可回读匹配的依据 | 30 |
| 依据值匹配比例 | 1.0 |
| 命名地点出现次数 | 29 |
| 来源匹配次数 | 26 |
| 给定来源覆盖率 | 0.8966 |

严重问题按类型、方案、活动ID集合及来源进行一对一匹配；报价与路线缺失等提示也必须符合预期。
依据值匹配仅表示引用的数据存在，不证明模型推理正确。审核错误单独记为 error，不计作通过或严重冲突。

| 案例 | 模式 | 预期 / 实际状态 | 结果 |
|---|---|---|---|
| known-valid：完整来源、时间和已知费用，无硬冲突 | rules | passed / passed | 符合预期 |
| budget-over：已知活动费用超出本次明确总预算 | rules | blocked / blocked | 符合预期 |
| formatted-money：货币符号与千位分隔仍能核算 | rules | blocked / blocked | 符合预期 |
| group-price：总价已经包含两人，不能再次乘人数 | rules | passed / passed | 符合预期 |
| alternative-styles：备选方案不能累加为同一笔消费 | rules | passed / passed | 符合预期 |
| unknown-price：未知零元价格不能当成免费或超支证据 | rules | warning / warning | 符合预期 |
| stale-total：旧汇总不能覆盖全部已知活动价格 | rules | warning / warning | 符合预期 |
| overlap：同一方案同一天的活动有时间重叠 | rules | blocked / blocked | 符合预期 |
| route-too-long：真实交通耗时大于活动间隙 | rules | blocked / blocked | 符合预期 |
| route-exact-boundary：间隙恰好等于真实路线耗时，不误判 | rules | passed / passed | 符合预期 |
| route-missing：无路线耗时只能提示，不能编造硬冲突 | rules | warning / warning | 符合预期 |
| venue-not-in-source：完整的非空候选中找不到所选地点 | rules | blocked / blocked | 符合预期 |
| source-missing：类别来源为空不能断言地点不存在 | rules | warning / warning | 符合预期 |
| wrong-branch：不同分店不能只凭品牌名匹配 | rules | blocked / blocked | 符合预期 |
| source-alias：仅使用实际来源提供的别名匹配 | rules | passed / passed | 符合预期 |
| overnight-train：跨日列车不误当成同日时间倒置 | rules | passed / passed | 符合预期 |
| flight-buffer：活动结束到航班出发不足项目120分钟预留 | rules | blocked / blocked | 符合预期 |
| empty-plan：没有任何活动的方案不能通过 | rules | blocked / blocked | 符合预期 |
| hybrid-rule-preserved：模型替身返回通过不能覆盖规则超支 | hybrid_fixture | blocked / blocked | 符合预期 |
| hybrid-no-issues：规则无问题且模型替身通过，完整审核通过 | hybrid_fixture | passed / passed | 符合预期 |
| hybrid-explicit-allergy：模拟模型的明确忌口问题有真实输入依据 | hybrid_fixture | blocked / blocked | 符合预期 |
| hybrid-weather-unsupported：无天气证据的模拟严重判断降为建议 | hybrid_fixture | warning / warning | 符合预期 |
| hybrid-model-error：模拟模型超时保留未知报价规则提示，不能显示通过 | hybrid_fixture | error / error | 符合预期 |
| hybrid-weather-evidence：模拟天气风险引用实际预报值，保留严重问题 | hybrid_fixture | blocked / blocked | 符合预期 |
| selected-meal-index-zero：餐厅第一个选项索引0是有效选择，不误当成未选定 | rules | passed / passed | 符合预期 |

## 复现

在项目根目录运行：

```bash
python3 -S ValidateAgent/evaluate.py
python3 -S ValidateAgent/evaluate.py --check
```

第一条重建 report.json 与 report.md；第二条核对已提交报告是否与当前代码、案例一致。失败时退出码为1。
完整问题、定位、来源指标与依据见同目录 report.json。

## 后续真实验收

保存实际搜索快照、本次需求、模型版本与输出；由组员独立标注预算、时间、偏好、天气和来源问题。
在同一组人工标注上比较规则、模型和组合审核的误报、漏报、状态、耗时与调用次数。
真实用户偏好、营业时间与天气推理的质量，以及网站交互，尚需手动验收；不能用这份合成报告替代。
