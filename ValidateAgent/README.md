# ValidateAgent - 旅行方案审核 agent

职责：审核 PlanAgent 生成的方案（预算、交通、用户喜好、天气、时间、完整性），
发现问题则退回让 PlanAgent 修改。

## 安装

```bash
cd ValidateAgent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 DeepSeek key
```

## 使用

### 单独审核一个方案

```bash
cat validate_input.json | python validate.py
```

输入结构：

```json
{
  "plan": {...PlanAgent 输出...},
  "profile": {...},
  "search": {...},
  "basic": {...},
  "answers": [...]
}
```

### 审核循环（生成 → 审核 → 退回修改）

```bash
cat loop_input.json | python run.py
```

`run.py` 会循环：PlanAgent 生成 → ValidateAgent 审核，不过就把 feedback 退回 PlanAgent
重新生成，最多 3 轮，输出最终方案 + 审核历史。

## 输出结构

```json
{
  "passed": true,
  "issues": [
    {"severity": "medium", "type": "预算", "detail": "…", "suggestion": "…"}
  ],
  "feedback": "给 PlanAgent 的总体修改建议"
}
```
