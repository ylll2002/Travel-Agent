import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import ts from 'typescript';

// Run the actual TS module with the existing compiler; no test-framework dependency.
const source = await readFile(new URL('../src/lib/planReview.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { confirmationReason, issueTarget, reviewHeading, currentAudit, currentPlanAudit, reviewMessage, canAutoRepair, advanceReviewProgress } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
const passed = { schema_version: 1, plan_revision: 4, status: 'passed', passed: true, issues: [] };
const issue = { severity: 'high', detail: '赶不上列车' };

test('editing a new revision never reuses the preceding passing verdict', () => {
  assert.equal(currentAudit(passed, 5), null);
  assert.equal(reviewMessage(currentAudit(passed, 5)), '审核暂未完成，当前版本需要重新审核。');
});

test('new blocked verdict reports the need to edit without copying problem details into chat', () => {
  const audit = { ...passed, plan_revision: 5, status: 'blocked', passed: false, issues: [issue] };
  assert.equal(currentAudit(audit, 5), audit);
  assert.equal(reviewMessage(audit), '当前行程需要调整，请查看审核窗口的审核结果。');
  assert.ok(!reviewMessage(audit).includes('赶不上列车'));
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
  assert.equal(reviewMessage(currentAudit(audit, 5)), '当前行程已通过审核，核实事项请查看审核窗口。');
  assert.ok(!reviewMessage(audit).includes('报价待核实'));
  assert.equal(currentAudit(audit, 6), null);
});


const blocks = [
  { id: 'v1', plan_style: '经典', day: 1, date: '2026-10-10', name: '博物馆' },
  { id: 'v2', plan_style: '经典', day: 2, date: '2026-10-11', name: '公园' },
  { id: 'v3', plan_style: '轻松', day: 1, date: '2026-10-10', name: '酒店' },
];
const reviewed = { revision: 4, audit: passed, blocks };

test('confirmation permits current pass and suggestions, blocks unreviewed/error/severe/pending/selection', () => {
  assert.equal(confirmationReason(reviewed), '');
  assert.equal(confirmationReason({ ...reviewed, audit: { ...passed, status: 'warning', issues: [{severity: 'medium', detail: '报价待核实'}] } }), '');
  for (const plan of [null, { ...reviewed, revision: 5 }, { ...reviewed, audit: null },
    { ...reviewed, audit: { ...passed, status: 'error', passed: false } },
    { ...reviewed, audit: { ...passed, status: 'blocked', passed: false, issues: [issue] } },
    { ...reviewed, review_pending: true },
  ]) assert.notEqual(confirmationReason(plan), '');
  assert.notEqual(confirmationReason(reviewed, true), '');
  assert.notEqual(confirmationReason(reviewed, false, 1), '');
});

test('issue navigation derives names, day and style from actual IDs without changing selection', () => {
  const input = { block_ids: ['v2', 'v1'], plan_style: '经典', day: 999 };
  assert.deepEqual(issueTarget(input, blocks), {style: '经典', day: 1, ids: ['v1', 'v2'], names: ['博物馆', '公园']});
  assert.deepEqual(input.block_ids, ['v2', 'v1']);
  assert.equal(issueTarget({ block_ids: ['missing'], plan_style: null }, blocks), null);
  assert.equal(issueTarget({ block_ids: ['v3'], plan_style: '经典', day: 9 }, blocks), null);
  assert.deepEqual(issueTarget({ block_ids: [], plan_style: '经典', day: 2 }, blocks),
    {style: '经典', day: 2, ids: [], names: []});
});

test('pending/obsolete verdict cannot render passed status, error is distinct from blocked', () => {
  assert.equal(reviewHeading(reviewed).status, 'passed');
  assert.equal(reviewHeading({...reviewed, revision: 5}).title, '尚未审核');
  assert.equal(reviewHeading({...reviewed, review_pending: true}).title, '正在审核');
  assert.equal(reviewHeading({...reviewed, audit: {...passed, status: 'error', passed: false}}).title, '审核未完成');
});

// Render the actual component with React's existing server renderer.
const libUrl = `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`;
const panelSource = await readFile(new URL('../src/components/PlanReviewPanel.tsx', import.meta.url), 'utf8');
const panelCode = ts.transpileModule(panelSource, { compilerOptions: {
  module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX,
} }).outputText.replaceAll('../lib/planReview', libUrl).replaceAll('react/jsx-runtime', import.meta.resolve('react/jsx-runtime'));
const { PlanReviewPanel } = await import(`data:text/javascript;base64,${Buffer.from(panelCode).toString('base64')}`);
const { createElement } = await import('react');
const { renderToStaticMarkup } = await import('react-dom/server');
const render = plan => renderToStaticMarkup(createElement(PlanReviewPanel, {plan, busy: false, onLocate() {}, onRetry() {}}));

test('panel renders current problem location, trusted origin and evidence, escapes text', () => {
  const audit = {...passed, status: 'blocked', passed: false, checks: {rules:'completed',model:'skipped'}, issues: [{
    ...issue, type:'交通', source:'rule', plan_style:'经典', day:1, date:'2026-10-10', block_ids:['v1'],
    detail:'<script>不够时间</script>', suggestion:'延后活动', evidence:[{path:'plan.blocks[0].time',value:'09:00-11:00'}],
  }]};
  const html = render({...reviewed, audit});
  for (const text of ['需要修改', '博物馆', '第 1 天', '规则检查', '定位活动', '查看依据', '未执行']) assert.ok(html.includes(text));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('review error retains rule issues and retry; pending state hides obsolete findings', () => {
  const plan = {...reviewed, audit:{...passed,status:'error',passed:false,issues:[{...issue,type:'预算',severity:'medium',source:'rule',block_ids:[],suggestion:'核实报价'}]}};
  assert.match(render(plan), /审核未完成/);
  assert.match(render(plan), /重试审核/);
  assert.match(render(plan), /赶不上列车/);
  const pending = render({...plan,review_pending:true});
  assert.match(pending, /正在审核/);
  assert.ok(!pending.includes('赶不上列车'));
  assert.ok(!pending.includes('重试审核'));
});


test('unknown-price suggestion permits confirmation; model failure is a separate reason to retry', () => {
  const audit = {...passed, status:'warning', passed:true, checks:{rules:'completed',model:'completed'},
    issues:[{severity:'medium',type:'预算',source:'rule',detail:'报价未知，预订前核实',suggestion:'核实报价',block_ids:[],actionable:false}]};
  assert.equal(confirmationReason({...reviewed, audit}), '');
  assert.match(render({...reviewed, audit}), /未知报价仅作核实提示/);
  assert.notEqual(confirmationReason({...reviewed, audit:{...audit,status:'error',passed:false,error:'模型审核未返回有效JSON'}}), '');
});


test('suggestion details stay in the plan panel while chat only announces the result', () => {
  const audit = {...passed, status:'warning', issues:[{
    severity:'medium',type:'偏好',source:'model',block_ids:['v1'],plan_style:'经典',day:1,
    detail:'餐饮可进一步匹配杭帮菜',suggestion:'选择现有杭帮菜候选',evidence:[],actionable:true,
  }]};
  assert.match(render({...reviewed, audit}), /餐饮可进一步匹配杭帮菜/);
  assert.match(render({...reviewed, audit}), /选择现有杭帮菜候选/);
  assert.ok(!reviewMessage(audit).includes('杭帮菜'));
  assert.ok(!reviewMessage(audit).includes('审核建议：'));
});


test('only verified actionable high failures start automatic repair', () => {
  const bad = {...passed,status:'blocked',passed:false,issues:[{...issue,actionable:true}]};
  assert.equal(canAutoRepair(bad),true);
  for (const audit of [null,passed,{...bad,status:'error',error:'timeout'},
    {...bad,issues:[{...issue,actionable:false}]},{...passed,status:'warning',issues:[{...issue,severity:'medium',actionable:true}]}]) {
    assert.equal(canAutoRepair(audit),false);
  }
});

test('live review keeps failed reasons during repair and hides old verdict on the next snapshot', () => {
  const bad = {...passed,status:'blocked',passed:false,issues:[{...issue,actionable:true,block_ids:['v1'],suggestion:'提前结束活动'}]};
  let progress=advanceReviewProgress(null,{stage:'reviewing',plan:{revision:4}});
  progress=advanceReviewProgress(progress,{stage:'repairing',plan:{revision:4},audit:bad,repair_count:0,repair_limit:2});
  const html=renderToStaticMarkup(createElement(PlanReviewPanel,{plan:{...reviewed,audit:bad},busy:true,progress,onLocate(){},onRetry(){}}));
  assert.match(html,/正在自动修复/);
  assert.match(html,/第 1\/2 次修复/);
  assert.match(html,/赶不上列车/);
  progress=advanceReviewProgress(progress,{stage:'reviewing',plan:{revision:5},repair_count:1});
  assert.equal(progress.entries[1].reasons[0],'赶不上列车');
  const next=renderToStaticMarkup(createElement(PlanReviewPanel,{plan:{...reviewed,revision:5,audit:null,review_pending:true},busy:true,progress,onLocate(){},onRetry(){}}));
  assert.match(next,/正在审核/);
  assert.ok(!next.includes('ta-review-issue-high'));
  assert.notEqual(confirmationReason({...reviewed,revision:5,audit:null,review_pending:true},true),'');
});

test('reaching the automatic repair limit never unlocks confirmation', () => {
  const bad={...passed,status:'blocked',passed:false,issues:[issue]};
  const progress=advanceReviewProgress(null,{stage:'blocked',plan:{revision:4},audit:bad,repair_count:2,stop_reason:'limit'});
  const html=renderToStaticMarkup(createElement(PlanReviewPanel,{plan:{...reviewed,audit:bad},busy:false,progress,onLocate(){},onRetry(){},onRepair(){}}));
  assert.match(html,/已完成 2 次自动修复/);
  assert.match(html,/再次自动修复/);
  assert.notEqual(confirmationReason({...reviewed,audit:bad}),'');
});
