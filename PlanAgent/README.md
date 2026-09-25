# PlanAgent - 旅行计划生成 agent

职责：读取 SearchAgent 的结构化输出（天气/酒店/景点/促销），生成一份逐日旅行计划。

## 安装

```bash
cd PlanAgent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 DeepSeek key
```

## 使用

先跑 SearchAgent 拿结构化结果，再喂给 PlanAgent：

```bash
echo '{"destination":"宁波","start_date":"2026-10-01","end_date":"2026-10-03"}' \
  | python ../SearchAgent/search.py \
  | python plan.py
```

也可以把 SearchAgent 的结果存成文件再喂：

```bash
python ../SearchAgent/search.py "宁波 10月1日到10月3日" > result.json
python plan.py result.json
```

## 输出结构

```json
{
  "destination": "宁波",
  "start_date": "2026-10-01",
  "end_date": "2026-10-03",
  "days": 3,
  "weather_summary": "…",
  "plans": [
    {
      "style": "经典人气",
      "summary": "…",
      "itinerary": [
        {
          "day": 1,
          "date": "2026-10-01",
          "theme": "…",
          "activities": [{"time": "上午", "name": "…", "note": "…"}],
          "hotel": "…",
          "meals": ["…"],
          "tips": "…"
        }
      ],
      "recommended_hotels": ["…"],
      "recommended_promotions": ["…"],
      "notes": "…"
    },
    {
      "style": "小众深度",
      "summary": "…",
      "itinerary": ["…"],
      "recommended_hotels": ["…"],
      "recommended_promotions": ["…"],
      "notes": "…"
    }
  ]
}
```
