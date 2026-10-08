"""Reproducible synthetic regression evaluation. Default needs only stdlib; no network."""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ValidateAgent.context import plan_for_model, search_for_model
from ValidateAgent.evidence import read_path, same_value, verify_model_issues
from ValidateAgent.rules import validate_rules
from shared.audit import combine_audits, failed_audit, normalize_audit

DATASET = Path(__file__).parent / "evaluation" / "cases.json"


def signature(issue):
    return (issue["type"], issue.get("plan_style"), tuple(sorted(issue.get("block_ids", []))), issue["source"])


def review_case(case):
    """Evaluate production rules and shared evidence/union functions using fixed model outputs.

    SDK calls/prompt quality are deliberately outside this offline evaluation.
    """
    data = deepcopy(case["input"])
    plan, search, basic = data["plan"], data.get("search", {}), data.get("basic", {})
    rule = validate_rules(plan, search=search, basic=basic)
    if case["mode"] == "rules":
        return rule
    if case["mode"] != "hybrid_fixture":
        raise ValueError("unsupported evaluation mode")
    if rule.get("error") or any(i["severity"] == "high" for i in rule["issues"]):
        return combine_audits(rule, plan=plan)
    raw = case["model_fixture"]
    model = failed_audit(raw["error"], "模拟模型故障", plan) if raw.get("error") else normalize_audit(raw, plan)
    original = {"plan": plan, "search": search, "basic": basic}
    sent = {**original, "plan": plan_for_model(plan), "search": search_for_model(search)}
    return combine_audits(rule, verify_model_issues(model, original, sent), plan)


def evaluate(dataset):
    cases = dataset["cases"]
    ids = [c["id"] for c in cases]
    if not cases or len(ids) != len(set(ids)):
        raise ValueError("case IDs must be nonempty and unique")
    rows, tp, fp, fn, false_blocks, missed_blocks, evidence_total, evidence_valid = [], 0, 0, 0, 0, 0, 0, 0
    named, matched = 0, 0
    for case in cases:
        audit = review_case(case)
        expected = case["expected"]
        actual_high = Counter(signature(i) for i in audit["issues"] if i["severity"] == "high")
        expected_high = Counter(signature(i) for i in expected["high"])
        hit = sum((actual_high & expected_high).values())
        extra = sum((actual_high - expected_high).values())
        missing = sum((expected_high - actual_high).values())
        tp += hit; fp += extra; fn += missing
        warnings = sorted({i["type"] for i in audit["issues"] if i["severity"] != "high"})
        valid = audit["status"] == expected["status"] and not extra and not missing and warnings == sorted(expected["warning_types"])
        false_blocks += audit["status"] == "blocked" and expected["status"] != "blocked"
        missed_blocks += audit["status"] != "blocked" and expected["status"] == "blocked"
        context = case["input"]
        for issue in audit["issues"]:
            for evidence in issue.get("evidence", []):
                evidence_total += 1
                try:
                    evidence_valid += same_value(read_path(context, evidence["path"]), evidence["value"])
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
        ground = audit.get("rule_summary", {}).get("grounding", {})
        named += ground.get("named_count", 0); matched += ground.get("matched_count", 0)
        rows.append({"id": case["id"], "description": case["description"], "mode": case["mode"],
                     "expected_status": expected["status"], "actual_status": audit["status"], "ok": valid,
                     "extra_high_count": extra, "missing_high_count": missing, "warning_types": warnings,
                     "expected_warning_types": expected["warning_types"], "audit": audit})
    ratio = lambda n, d: round(n / d, 4) if d else None
    encoded = json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"schema_version": 1, "evaluation_kind": "offline_synthetic_regression",
            "dataset_sha256": hashlib.sha256(encoded).hexdigest(),
            "limitations": ["规则结果不代表完整模型审核通过。", "模型替身用于验证合并与证据保护，不评价真实模型语义质量。",
                            "合成案例不代表实际旅游信息；来源覆盖率只衡量给定快照。", "指标基于人工案例标签，不是生产准确率或独立泛化测试。"],
            "metrics": {"case_count": len(rows), "passed_cases": sum(r["ok"] for r in rows),
                        "status_accuracy": ratio(sum(r["actual_status"] == r["expected_status"] for r in rows), len(rows)),
                        "high_true_positive": tp, "high_false_positive": fp, "high_false_negative": fn,
                        "high_precision": ratio(tp, tp + fp), "high_recall": ratio(tp, tp + fn),
                        "false_blocked_cases": false_blocks, "missed_blocked_cases": missed_blocks,
                        "evidence_count": evidence_total, "verifiable_evidence_count": evidence_valid,
                        "evidence_value_match_rate": ratio(evidence_valid, evidence_total),
                        "named_occurrences": named, "matched_occurrences": matched, "source_coverage": ratio(matched, named)},
            "cases": rows}


def markdown(report):
    m = report["metrics"]
    lines = ["# Validate Agent 离线回归评估", "", "本报告由固定合成案例自动生成。默认运行不调用模型、不联网。", "",
             "**这不是实际模型准确率报告。hybrid_fixture 使用预置模型输出，仅验证合并和证据处理。**", "",
             "数据集 SHA256：`" + report["dataset_sha256"] + "`", "", "| 指标 | 结果 |", "|---|---:|"]
    labels = {"case_count":"案例数", "passed_cases":"符合全部预期的案例", "status_accuracy":"状态匹配比例",
              "high_true_positive":"严重问题正确命中", "high_false_positive":"严重问题误报", "high_false_negative":"严重问题漏报",
              "high_precision":"严重问题精确率（此案例集）", "high_recall":"严重问题召回率（此案例集）",
              "false_blocked_cases":"错误阻断案例", "missed_blocked_cases":"应阻断却未阻断案例",
              "evidence_count":"问题依据条数", "verifiable_evidence_count":"可回读匹配的依据",
              "evidence_value_match_rate":"依据值匹配比例", "named_occurrences":"命名地点出现次数",
              "matched_occurrences":"来源匹配次数", "source_coverage":"给定来源覆盖率"}
    lines += [f"| {label} | {m[key] if m[key] is not None else '无分母'} |" for key, label in labels.items()]
    lines += ["", "严重问题按类型、方案、活动ID集合及来源进行一对一匹配；报价与路线缺失等提示也必须符合预期。",
              "依据值匹配仅表示引用的数据存在，不证明模型推理正确。审核错误单独记为 error，不计作通过或严重冲突。", "",
              "| 案例 | 模式 | 预期 / 实际状态 | 结果 |", "|---|---|---|---|"]
    lines += [f"| {r['id']}：{r['description']} | {r['mode']} | {r['expected_status']} / {r['actual_status']} | {'符合预期' if r['ok'] else '不符合预期'} |" for r in report["cases"]]
    lines += ["", "## 复现", "", "在项目根目录运行：", "", "```bash", "python3 -S ValidateAgent/evaluate.py", "python3 -S ValidateAgent/evaluate.py --check", "```", "",
              "第一条重建 report.json 与 report.md；第二条核对已提交报告是否与当前代码、案例一致。失败时退出码为1。",
              "完整问题、定位、来源指标与依据见同目录 report.json。", "", "## 后续真实验收", "",
              "保存实际搜索快照、本次需求、模型版本与输出；由组员独立标注预算、时间、偏好、天气和来源问题。",
              "在同一组人工标注上比较规则、模型和组合审核的误报、漏报、状态、耗时与调用次数。",
              "真实用户偏好、营业时间与天气推理的质量，以及网站交互，尚需手动验收；不能用这份合成报告替代。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DATASET)
    parser.add_argument("--output-dir", type=Path, default=DATASET.parent)
    parser.add_argument("--check", action="store_true", help="Compare with saved reports; write nothing")
    args = parser.parse_args()
    report = evaluate(json.loads(args.cases.read_text(encoding="utf-8")))
    outputs = {"report.json": json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", "report.md": markdown(report)}
    mismatches = []
    if args.check:
        for name, value in outputs.items():
            path = args.output_dir / name
            if not path.exists() or path.read_text(encoding="utf-8") != value:
                mismatches.append(name)
    else:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name, value in outputs.items():
            (args.output_dir / name).write_text(value, encoding="utf-8")
    metrics = report["metrics"]
    print(json.dumps({"metrics": metrics, "report_mismatches": mismatches}, ensure_ascii=False, indent=2))
    return 0 if metrics["passed_cases"] == metrics["case_count"] and metrics["evidence_count"] == metrics["verifiable_evidence_count"] and not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
