import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import ts from 'typescript';

const source = await readFile(new URL('../src/lib/planAdd.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { matchingSpots, addTimeRange, ambiguousDeleteChoices, mutationErrorMessage } = await import(
  `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('named attraction lookup keeps the requested city attraction and excludes similarly named shops', () => {
  const spots = [
    { title: '灵隐寺-停车场' },
    { title: '杭州灵隐寺景区', lng: 120.1, lat: 30.2 },
    { title: '灵隐寺' },
  ];
  assert.deepEqual(matchingSpots('灵隐寺', '杭州', spots), spots.slice(1));
  assert.deepEqual(matchingSpots('西湖', '杭州', [{ title: '西湖天地' }]), []);
});

test('optional start time becomes a backend-valid time span', () => {
  assert.equal(addTimeRange(''), '');
  assert.equal(addTimeRange('14:00'), '14:00-15:00');
  assert.equal(addTimeRange('23:00'), '23:00-24:00');
  assert.equal(addTimeRange('14:00-16:00'), '14:00-16:00');
  assert.equal(addTimeRange('23:30'), null);
  assert.equal(addTimeRange('25:00'), null);
});

test('ambiguous delete candidates are surfaced while unrelated errors are left alone', () => {
  const candidate = { id: 'b1', name: '西湖', day: 1, time: '09:00-10:00', plan_style: '经典' };
  const body = JSON.stringify({ detail: { code: 'ambiguous_delete', candidates: [candidate, { ...candidate, id: 'b2', day: 2 }] } });
  assert.deepEqual(ambiguousDeleteChoices(new Error(`请求失败 (422): ${body}`)).map(item => item.id), ['b1', 'b2']);
  assert.equal(ambiguousDeleteChoices(new Error('请求失败 (422): {"detail":"时间冲突"}')), null);
  assert.equal(mutationErrorMessage(new Error('请求失败 (422): {"detail":"当天已没有足够空档，请换一天"}')),
    '当天已没有足够空档，请换一天');
});
