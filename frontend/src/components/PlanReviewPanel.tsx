import { currentPlanAudit, issueTarget, reviewHeading } from '../lib/planReview';
import type { AuditIssue, ReviewPlan, ReviewProgress } from '../lib/planReview';

type Props = { plan: ReviewPlan; busy: boolean; onLocate: (issue: AuditIssue) => void; onRetry: () => void; onRepair?: () => void; progress?: ReviewProgress | null };
const severityLabel = { high: '需要修改', medium: '建议核实', low: '提示' };
const checkLabel = { completed: '已完成', skipped: '未执行', error: '失败' };

export function PlanReviewPanel({ plan, busy, onLocate, onRetry, onRepair, progress }: Props) {
  let heading = reviewHeading(plan);
  if (progress?.stage === 'planning') heading = {status:'pending',title:'正在生成方案',description:'方案生成后将自动审核和修复。'};
  if (progress?.stage === 'reviewing') heading = {status:'pending',title:'正在审核',description:'正在检查当前版本的费用、时间、来源和旅行需求。'};
  if (progress?.stage === 'repairing') heading = {status:'blocked',title:'正在自动修复',description:`已将严重问题交给规划助手，正在进行第 ${progress.repairCount + 1}/${progress.repairLimit} 次修复。`};
  if (progress?.stage === 'cancelled') heading = {status:'pending',title:'处理已停止',description:'已保留最近生成的行程，你仍可自行确认计划。'};
  if (progress?.stopReason === 'limit' && !plan.audit?.passed) heading.description = `已完成 ${progress.repairCount} 次自动修复，仍有以下问题。行程已保留，可以再次尝试自动修复。`;
  const audit = currentPlanAudit(plan);
  if (progress?.stage === 'error' && !audit) heading = {status:'error',title:'处理未完成',description:'已保留现有行程，请重试。具体原因见处理过程。'};
  const grounding = audit?.rule_summary?.grounding as { named_count?: number; matched_count?: number; coverage?: number | null } | undefined;
  const hasCoverage = typeof grounding?.coverage === 'number' && Number.isFinite(grounding.coverage);
  const issues = [...(audit?.issues ?? [])].sort((a, b) =>
    ['high', 'medium', 'low'].indexOf(a.severity) - ['high', 'medium', 'low'].indexOf(b.severity));
  return (
    <section className={`ta-review ta-review-${heading.status}`} aria-label="行程审核">
      <div className="ta-review-header" role="status" aria-live="polite">
        <strong>{heading.title}</strong>
        {plan.revision != null && <span>行程版本 {plan.revision}</span>}
      </div>
      <p>{heading.description}</p>
      {audit?.status === 'error' && audit.error && <p>原因：{audit.error}</p>}
      <small>审核覆盖全部备选方案；未知报价仅作核实提示，不会单独阻止确认。</small>
      {audit?.checks && <p className="ta-review-checks">规则检查：{checkLabel[audit.checks.rules]} · 模型审核：{checkLabel[audit.checks.model]}</p>}
      {hasCoverage && <p className="ta-review-checks">来源匹配：{grounding?.matched_count}/{grounding?.named_count} 个条目（{Math.round(grounding!.coverage! * 100)}%）<br /><small>仅衡量当前搜索数据的覆盖率。</small></p>}
      {issues.length > 0 && (
        <details key={`${plan.revision}-${audit?.status}`} open={audit?.status === 'blocked' || audit?.status === 'error'}>
          <summary>查看 {issues.length} 条问题与建议</summary>
          <ol className="ta-review-issues">
            {issues.map((issue, index) => {
              const target = issueTarget(issue, plan.blocks);
              return <li key={index} className={`ta-review-issue ta-review-issue-${issue.severity}`}>
                <div className="ta-review-issue-meta"><strong>{severityLabel[issue.severity]}</strong> · {issue.type} · {issue.source === 'rule' ? '规则检查' : '模型审核'}</div>
                <div>{[issue.plan_style, issue.day ? `第 ${issue.day} 天` : '', issue.date].filter(Boolean).join(' · ') || '整体行程'}{target?.names.length ? ` · ${target.names.join('、')}` : ''}</div>
                <p>{issue.detail}</p>
                {issue.suggestion && <p>建议：{issue.suggestion}</p>}
                {Array.isArray(issue.evidence) && issue.evidence.length > 0 && <details className="ta-review-evidence"><summary>查看依据</summary><ul>{issue.evidence.map((e, n) => <li key={n}><code>{e.path}</code><span>{JSON.stringify(e.value)}</span></li>)}</ul></details>}
                {target && <button type="button" disabled={busy} onClick={() => onLocate(issue)}>定位{target.ids.length ? '活动' : '方案日期'}</button>}
              </li>;
            })}
          </ol>
        </details>
      )}
      {progress && progress.entries.length > 0 && <details className="ta-review-timeline">
        <summary>查看处理过程（{progress.entries.length} 步）</summary>
        <ol>{progress.entries.map((entry, index) => <li key={index}>
          <strong>{({planning:'生成方案',reviewing:'审核中',repairing:'未通过，交给规划助手修复',passed:'审核通过',warning:'审核通过，有建议',blocked:'仍需修复',error:'处理失败',cancelled:'已停止'} as Record<string,string>)[entry.stage]}</strong>
          {entry.revision != null && <span> · 版本 {entry.revision}</span>}
          {entry.reasons.map((reason, n) => <p key={n}>{reason}</p>)}
        </li>)}</ol>
      </details>}
      {audit?.status === 'blocked' && onRepair && !plan.review_pending && <button type="button" disabled={busy} onClick={onRepair}>再次自动修复</button>}
      {!audit && !plan.review_pending && !plan.revision && <p>请重新生成正式行程以完成审核。</p>}
      {(!audit || audit.status === 'error') && !plan.review_pending && (
        <button type="button" disabled={busy || !Number.isSafeInteger(plan.revision) || !plan.revision || plan.revision < 1}
          onClick={onRetry}>重试审核</button>
      )}
    </section>
  );
}
