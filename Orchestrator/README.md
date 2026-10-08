# Orchestrator - 多 Agent 编排

用 LangGraph 把三个 agent 串成一条流水线：

```text
search ─┐
        ├→ plan → validate
prepare_memory ─┘    ↑       │
                     └─不通过─┘（携带 feedback，最多 1 轮）
```

## 安装

```bash
cd Orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 使用

```bash
echo '{
  "destination":"宁波","start_date":"2026-10-01","end_date":"2026-10-03",
  "profile":{...},"basic":{...},"answers":[...]
}' | python orchestrator.py
```

## 查看图

```bash
python orchestrator.py --graph
```
