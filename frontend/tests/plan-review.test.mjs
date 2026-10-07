import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import ts from 'typescript';

// Run the actual TS module with the existing compiler; no test-framework dependency.
const source = await readFile(new URL('../src/lib/planReview.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { currentAudit, currentPlanAudit, reviewMessage } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
const passed = { schema_version: 1, plan_revision: 4, status: 'passed', passed: true, issues: [] };
const issue = { severity: 'high', detail: '赶不上列车' };

test('editing a new revision never reuses the preceding passing verdict', () => {
  assert.equal(currentAudit(passed, 5), null);
  assert.equal(reviewMessage(currentAudit(passed, 5)), '审核暂未完成，当前版本需要重新审核。');
});

test('new blocked verdict replaces old success and reports the current problem', () => {
  const audit = { ...passed, plan_revision: 5, status: 'blocked', passed: false, issues: [issue] };
  assert.equal(currentAudit(audit, 5), audit);
  assert.equal(reviewMessage(audit), '审核提示：赶不上列车');
});

test('review failure after successful editing cannot look like a successful review', () => {
  const audit = { ...passed, plan_revision: 5, status: 'error', passed: false, error: '模型超时' };
  assert.equal(currentAudit(audit, 5), audit);
  assert.match(reviewMessage(audit), /未完成/);
});

test('pending edit hides the prior verdict; cancelled unchanged edit can restore it', () => {
  const snapshot = { revision: 4, audit: passed };
  assert.equal(currentPlanAudit({ ...snapshot, review_pending: true }), null);
  assert.equal(currentPlanAudit({ ...snapshot, review_pending: false }), passed);
});

test('missing review or invalid version stays unreviewed instead of defaulting to pass', () => {
  for (const revision of [undefined, null, true, 0, -1, 4.5]) assert.equal(currentAudit(passed, revision), null);
  assert.equal(currentAudit(undefined, 5), null);
});

test('contradictory verdict or malformed issues cannot become current review', () => {
  for (const audit of [
    { ...passed, issues: [issue] }, { ...passed, status: 'error' }, { ...passed, error: 'timeout' },
    { ...passed, passed: 'true' }, { ...passed, issues: [null] }, { ...passed, issues: {} },
  ]) assert.equal(currentAudit(audit, 4), null);
});

test('non-blocking suggestions belong to the matching new version only', () => {
  const audit = { ...passed, plan_revision: 5, status: 'warning', issues: [{ severity: 'medium', detail: '报价待核实' }] };
  assert.match(reviewMessage(currentAudit(audit, 5)), /报价待核实/);
  assert.equal(currentAudit(audit, 6), null);
});
