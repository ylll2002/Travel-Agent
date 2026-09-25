"""专业 Agent 的选择、并发执行和报告合并。"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.agent.agents.dining import dining_selection_agent
from app.agent.agents.planner import _needs_dining_report, _needs_transport_report
from app.agent.agents.transport import transport_accommodation_agent
from app.agent.state import SpecialistResult, TravelAgentState, TravelAgentUpdate


def run_specialist_batch(state: TravelAgentState) -> TravelAgentUpdate:
    """按当前状态并发运行互不依赖的专业任务。"""
    reports = state.get("agent_reports", {})
    tasks = {}
    if "dining" not in reports and _needs_dining_report(state):
        tasks["dining"] = dining_selection_agent
    if "transport" not in reports and _needs_transport_report(state):
        tasks["transport"] = transport_accommodation_agent

    if not tasks:
        return {"agent_response": "当前没有需要补充的专业规划任务。"}

    results: dict[str, TravelAgentUpdate] = {}
    task_records = [{"agent": name, "status": "running", "error": None} for name in tasks]
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(agent, state): name for name, agent in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as error:
                results[name] = {"specialist_results": [{
                    "agent": name,
                    "report": {"status": "failed", "summary": f"{name} Agent 执行失败", "recommendations": [], "evidence_refs": [], "uncertainties": [str(error)], "cost_items": [], "question_candidates": []},
                    "status": "failed", "error": str(error), "retryable": True,
                }]}

    merged_reports = dict(reports)
    specialist_results: list[SpecialistResult] = []
    response_parts: list[str] = []
    for name in tasks:
        result = results[name]
        for specialist_result in result.get("specialist_results", []):
            specialist_results.append(specialist_result)
            report = specialist_result.get("report")
            if report is not None:
                merged_reports[name] = report
        if result.get("agent_response"):
            response_parts.append(str(result["agent_response"]))

    completed = [item["agent"] for item in specialist_results if item.get("status", "complete") != "failed"]
    task_records = [{**task, "status": "completed" if task["agent"] in completed else "failed"} for task in task_records]
    return {
        "agent_reports": merged_reports,
        "specialist_results": specialist_results,
        "agent_tasks": task_records,
        "workflow_events": [{"event": "specialist_batch_completed", "agents": list(tasks), "completed": completed}],
        "current_stage": "specialists",
        "agent_response": "；".join(response_parts),
    }
