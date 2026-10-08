# Orchestrator - 多 Agent 编排

用 LangGraph 把三个 agent 串成一条流水线：

```text
search ─┐
        ├→ plan → validate
prepare_memory ─┘    ↑       │
                     └─不通过─┘（携带严重问题反馈和当前行程，最多自动修复 2 次）
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


网站流式模式 `--stream` 会依次推送方案快照、审核结果、自动修复阶段和最终结论。
`--stream --repair` 从传入的完整 plan/search/basic/review_context 开始重新审核，不重复搜索或用户修改指令。
只修复已核实且 actionable=true 的 high 问题；普通建议和服务错误不触发循环。
修复失败保留最近的完整行程，达到上限仍如实显示未通过，不能恢复旧审核结果。
