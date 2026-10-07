import { currentPlanAudit, issueTarget, reviewHeading } from '../lib/planReview';
import type { AuditIssue, ReviewPlan } from '../lib/planReview';

type Props = { plan: ReviewPlan; busy: boolean; onLocate: (issue: AuditIssue) => void; onRetry: () => void };
const severityLabel = { high: '需要修改', medium: '建议核实', low: '提示' };
const checkLabel = { completed: '已完成', skipped: '未执行', error: '失败' };

export function PlanReviewPanel({ plan, busy, onLocate, onRetry }: Props) {
  const heading = reviewHeading(plan);
  const audit = currentPlanAudit(plan);
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
      <small>审核覆盖全部备选方案；来源缺失和未知报价仍需核实。</small>
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
      {!audit && !plan.review_pending && !plan.revision && <p>请重新生成正式行程以完成审核。</p>}
      {(!audit || audit.status === 'error') && !plan.review_pending && (
        <button type="button" disabled={busy || !Number.isSafeInteger(plan.revision) || !plan.revision || plan.revision < 1}
          onClick={onRetry}>重试审核</button>
      )}
    </section>
  );
}
