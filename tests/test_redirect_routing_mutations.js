'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const {
  counts, names, mutations, replaceOnce, withMutation, requireVerdict, verify,
} = require('../scripts/verify_redirect_routing_mutations');

function verdict(indices, failed = false) {
  const rows = indices.flatMap((i) => Array.from({ length: counts[i] }, (_, j) => (
    `tests/test_redirect_chain_backend_and_agent.py::${names[i]}[${j}] ${failed ? 'FAILED' : 'PASSED'} [100%]`
  )));
  const total = indices.reduce((n, i) => n + counts[i], 0);
  return { status: failed ? 1 : 0, stdout: `${rows.join('\n')}\n${total} ${failed ? 'failed' : 'passed'} in 0.1s` };
}

function privateSources(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-mutation-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'src'));
  for (const file of ['redirect_sweep.py', 'roles.py', 'redirect_plan.py']) {
    fs.copyFileSync(path.resolve(__dirname, '../src', file), path.join(root, 'src', file));
  }
  return root;
}

test('mutation gate verifies baseline, both regressions, restorations and final baseline', (t) => {
  const root = privateSources(t);
  const originalSweep = fs.readFileSync(path.join(root, 'src/redirect_sweep.py'), 'utf8');
  let calls = 0;
  const results = verify(root, (cwd, indices) => {
    calls += 1;
    assert.equal(cwd, root);
    if (calls === 2) {
      const current = fs.readFileSync(path.join(root, 'src/redirect_sweep.py'), 'utf8');
      assert.equal(current, replaceOnce(originalSweep, mutations[0].edits[0].before,
        mutations[0].edits[0].after));
    }
    if (calls === 4) {
      for (const edit of mutations[1].edits) {
        assert.ok(fs.readFileSync(path.join(root, edit.file), 'utf8').includes(edit.after));
      }
    }
    return verdict(indices, calls === 2 || calls === 4);
  });
  assert.equal(calls, 6);
  assert.deepEqual(results, [
    'baseline: 21 passed',
    'cursor fallback: 4 failed; restored: 4 passed',
    'missing worker and placeholder: 14 failed; restored: 14 passed',
    'final: 21 passed',
  ]);
  assert.equal(fs.readFileSync(path.join(root, 'src/redirect_sweep.py'), 'utf8'), originalSweep);
});

test('all source bytes are restored when the mutation runner throws', (t) => {
  const root = privateSources(t);
  const before = mutations[1].edits.map((edit) => fs.readFileSync(path.join(root, edit.file)));
  assert.throws(() => withMutation(root, mutations[1], () => {
    throw new Error('runner timed out');
  }), /runner timed out/);
  for (const [i, edit] of mutations[1].edits.entries()) {
    assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), before[i]);
  }
});

test('an outdated second mutation anchor restores the first edit', (t) => {
  const root = privateSources(t);
  const file = path.join(root, mutations[1].edits[0].file);
  const before = fs.readFileSync(file);
  const mutation = { edits: [mutations[1].edits[0], {
    ...mutations[1].edits[1], before: 'removed source anchor',
  }] };
  assert.throws(() => withMutation(root, mutation, () => assert.fail('must not run')),
    /anchor must occur once/);
  assert.deepEqual(fs.readFileSync(file), before);
});

test('ambiguous mutation anchors refuse to edit', () => {
  assert.throws(() => replaceOnce('same same', 'same', 'changed'), /anchor must occur once/);
});

for (const [reason, result] of [
  ['missing pytest', { status: 1, stderr: 'No module named pytest' }],
  ['collection error', { status: 2, stdout: '1 error during collection' }],
  ['timeout', { status: null, error: new Error('ETIMEDOUT') }],
  ['no cases executed', { status: 0, stdout: 'no tests ran' }],
  ['one case silently missing', {
    ...verdict([0], true), stdout: verdict([0], true).stdout.replace(/^.*\n/, ''),
  }],
  ['one unexpected pass', {
    ...verdict([0], true), stdout: verdict([0], true).stdout.replace(' FAILED ', ' PASSED '),
  }],
  ['skipped cases', { ...verdict([0], true), stdout: verdict([0], true).stdout + '\n1 skipped' }],
]) {
  test(`gate rejects ${reason} as deliberate-break evidence`, () => {
    assert.throws(() => requireVerdict(result, [0], true));
  });
}
