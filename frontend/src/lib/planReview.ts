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

export function missingApiKeyMessage(error: unknown): string | undefined {
  return typeof error === 'string' && error.includes('未配置模型 API Key') ? error : undefined;
}

export function reviewMessage(audit: PlanAudit | null): string {
  const configurationMessage = missingApiKeyMessage(audit?.error);
  if (configurationMessage) return configurationMessage;
  if (!audit || audit.status === 'error') return '审核暂未完成，当前版本需要重新审核。';
  // Details and evidence live in the plan panel; chat only reports the outcome.
  if (!audit.passed) return '当前行程需要调整，请查看审核窗口的审核结果。';
  return audit.status === 'warning'
    ? '当前行程已通过审核，核实事项请查看审核窗口。'
    : '当前行程已通过审核。';
}

export function currentPlanAudit(plan: { revision?: unknown; audit?: unknown; review_pending?: boolean } | null): PlanAudit | null {
  return plan && !plan.review_pending ? currentAudit(plan.audit, plan.revision) : null;
}


export type ReviewPlan = {
  revision?: number; audit?: PlanAudit | null; review_pending?: boolean;
  blocks: Array<{ id: string; plan_style?: string; day: number; date?: string; name: string }>;
};

export function confirmationReason(plan: ReviewPlan | null, busy = false, selected = 0): string {
  if (busy || plan?.review_pending) return '正在处理行程，请稍候。';
  if (selected > 0) return '请先完成或取消选中的修改。';
  if (!plan || plan.blocks.length === 0) return '请先生成行程。';
  return '';
}

export function issueTarget(issue: AuditIssue, blocks: ReviewPlan['blocks']) {
  const ids = Array.isArray(issue.block_ids) ? issue.block_ids : [];
  const matched = blocks.filter(b => ids.includes(b.id) && (!issue.plan_style || b.plan_style === issue.plan_style));
  // Use actual blocks to locate issues; never fabricate an ID or a day.
  const first = matched[0] ?? blocks.find(b => b.plan_style === issue.plan_style &&
    (!issue.day || b.day === issue.day) && (!issue.date || b.date === issue.date));
  if (!first || !first.plan_style || !Number.isInteger(first.day) || first.day < 1) return null;
  return { style: first.plan_style, day: first.day,
    ids: matched.filter(b => b.plan_style === first.plan_style).map(b => b.id),
    names: matched.filter(b => b.plan_style === first.plan_style).map(b => b.name) };
}

export function reviewHeading(plan: ReviewPlan): { status: string; title: string; description: string } {
  if (plan.review_pending) return { status: 'pending', title: '正在审核', description: '处理完成后显示当前行程的审核结果。' };
  const audit = currentPlanAudit(plan);
  if (!audit) return { status: 'pending', title: '尚未审核', description: '当前行程没有有效的审核结果，你仍可自行确认计划。' };
  const descriptions = {
    passed: ['审核通过', '在现有数据范围内未发现问题。'],
    warning: ['审核通过，有建议', '未发现阻断问题，请在预订前核实以下建议。'],
    blocked: ['需要修改', '发现严重问题，建议查看并调整；你也可以直接确认计划。'],
    error: ['审核未完成', '现有行程已保留，请重试审核。已完成的规则检查仍显示在下方。'],
  };
  const [title, description] = descriptions[audit.status];
  return { status: audit.status, title, description };
}


export type ReviewProgress = {
  stage: 'planning' | 'reviewing' | 'repairing' | 'passed' | 'warning' | 'blocked' | 'error' | 'cancelled';
  repairCount: number;
  repairLimit: number;
  stopReason?: string;
  entries: Array<{ stage: string; revision?: number; reasons: string[] }>;
};

export function canAutoRepair(audit: PlanAudit | null, action?: unknown): boolean {
  return action !== 'delete' && !!audit && audit.status === 'blocked' && !audit.error && !audit.passed
    && audit.issues.some(issue => issue.severity === 'high' && issue.actionable);
}

export function advanceReviewProgress(previous: ReviewProgress | null, event: Record<string, unknown>): ReviewProgress | null {
  const stages = ['planning', 'reviewing', 'repairing', 'passed', 'warning', 'blocked', 'error', 'cancelled'];
  if (!stages.includes(String(event.stage))) return previous;
  const stage = event.stage as ReviewProgress['stage'];
  const plan = event.plan as {revision?: number} | undefined;
  const audit = currentAudit(event.audit, plan?.revision);
  const reasons = audit?.issues.filter(issue => issue.severity === 'high').map(issue => issue.detail) ?? [];
  if (audit?.error) reasons.push(audit.error);
  if (typeof event.error === 'string') reasons.push(event.error);
  const entry = {stage, revision: plan?.revision, reasons};
  const entries = [...(previous?.entries ?? [])];
  const last = entries[entries.length - 1];
  if (!last || last.stage !== stage || last.revision !== entry.revision || JSON.stringify(last.reasons) !== JSON.stringify(reasons)) entries.push(entry);
  return {stage, repairCount: Number(event.repair_count ?? previous?.repairCount ?? 0),
    repairLimit: Number(event.repair_limit ?? previous?.repairLimit ?? 2),
    stopReason: typeof event.stop_reason === 'string' ? event.stop_reason : undefined, entries};
}
