export type AuditIssue = {
  severity: 'high' | 'medium' | 'low';
  type: string;
  detail: string;
  suggestion: string;
  source: 'rule' | 'model';
  actionable: boolean;
  plan_style: string | null;
  day: number | null;
  date: string | null;
  block_ids: string[];
  evidence: Array<{ path: string; value: unknown }>;
};

export type PlanAudit = {
  schema_version: number;
  plan_revision: number;
  status: 'passed' | 'warning' | 'blocked' | 'error';
  passed: boolean;
  issues: AuditIssue[];
  error?: string;
  feedback?: string;
  checks?: { rules: 'completed' | 'error'; model: 'completed' | 'skipped' | 'error' };
  rule_summary?: Record<string, unknown>;
};

export type ReviewContext = { profile?: unknown; preferences?: unknown; recent_trips?: unknown };

export function currentAudit(value: unknown, revision: unknown): PlanAudit | null {
  if (!Number.isSafeInteger(revision) || Number(revision) < 1 || !value || typeof value !== 'object') return null;
  const audit = value as Partial<PlanAudit>;
  if (audit.schema_version !== 1 || audit.plan_revision !== revision || typeof audit.passed !== 'boolean'
      || !['passed', 'warning', 'blocked', 'error'].includes(String(audit.status)) || !Array.isArray(audit.issues)) return null;
  if (audit.issues.some((issue) => !issue || typeof issue.detail !== 'string'
      || !['high', 'medium', 'low'].includes(issue.severity))) return null;
  if (audit.passed !== (audit.status === 'passed' || audit.status === 'warning')
      || (audit.passed && audit.issues.some((issue) => issue.severity === 'high'))
      || (audit.passed && audit.error)) return null;
  return audit as PlanAudit;
}

export function reviewMessage(audit: PlanAudit | null): string {
  if (!audit || audit.status === 'error') return '审核暂未完成，当前版本需要重新审核。';
  const details = audit.issues.slice(0, 3).map((issue) => issue.detail).filter(Boolean).join('；');
  if (!audit.passed) return `审核提示：${details || '建议进一步检查行程安排。'}`;
  return details ? `审核建议：${details}` : '当前行程已通过审核。';
}

export function currentPlanAudit(plan: { revision?: unknown; audit?: unknown; review_pending?: boolean } | null): PlanAudit | null {
  return plan && !plan.review_pending ? currentAudit(plan.audit, plan.revision) : null;
}
