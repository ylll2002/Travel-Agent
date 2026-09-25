# QuestionnaireAgent - 偏好问卷生成 agent

职责：根据用户画像 + 基础信息（目的地/日期/预算等）+ SearchAgent 的搜索结果，
生成 4~6 道选择题问卷，把 PlanAgent 需要的偏好问得更细。

## 安装

```bash
cd QuestionnaireAgent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 DeepSeek key
```

## 使用

```bash
# 先跑 SearchAgent 拿到结果，再连同画像/基础信息喂给 QuestionnaireAgent
cat input.json | python questionnaire.py
```

其中 `input.json` 结构：

```json
{
  "profile": { "interests": ["美食购物"], "pace": ["慢节奏深度游"], "dietary": ["不吃辣"] },
  "search": { "poi": [...], "hotels": [...], "promotions": [...], "weather": {...} },
  "basic": { "destination": "宁波", "start_date": "2026-10-01", "end_date": "2026-10-03", "budget": "舒适型" }
}
```

## 输出结构

```json
{
  "questions": [
    {"id": "q1", "type": "multiple", "question": "以下景点你最想去哪几个？", "options": ["天一阁", "罗蒙环球乐园"], "max_select": 3},
    {"id": "q2", "type": "single", "question": "你更倾向住哪家酒店？", "options": ["万斓悦致", "全季百丈东路"], "max_select": null}
  ]
}
```
