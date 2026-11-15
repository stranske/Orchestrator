'use strict';

const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { edit, node, requireVerdict, verify } = require('../scripts/verify_issue_1138_mutation');

function verdict(failed = false) {
  return {
    status: failed ? 1 : 0,
    stdout: `${node} ${failed ? 'FAILED' : 'PASSED'} [100%]\n`
      + (failed ? "E   AssertionError: assert {'broken': True} == {}\n" : '')
      + `1 ${failed ? 'failed' : 'passed'} in 0.1s\n`,
  };
}

function sources(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'issue-1138-mutation-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'src'));
  fs.mkdirSync(path.join(root, 'tests'));
  fs.writeFileSync(path.join(root, edit.file), `# private mutation input\n${edit.before}\n`);
  fs.writeFileSync(path.join(root, 'tests/test_coverage_round_1138.py'), '# private test input\n');
  return root;
}

test('records a baseline, the intended assertion failure, and a restored pass', (t) => {
  const root = sources(t);
  const original = fs.readFileSync(path.join(root, edit.file));
  let calls = 0;
  const record = verify(root, (cwd) => {
    assert.equal(cwd, root);
    calls += 1;
    const source = fs.readFileSync(path.join(root, edit.file), 'utf8');
    assert.ok(source.includes(calls === 2 ? edit.after : edit.before));
    return verdict(calls === 2);
  });
  assert.equal(calls, 3);
  assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
  assert.equal(record.source_sha256, record.restored_source_sha256);
  const broken = original.toString('utf8').replace(edit.before, edit.after);
  assert.equal(record.broken_source_sha256, createHash('sha256').update(broken).digest('hex'));
  assert.notEqual(record.broken_source_sha256, record.source_sha256);
  assert.match(record.test_sha256, /^[a-f0-9]{64}$/);
  assert.deepEqual([record.baseline_exit, record.red_exit, record.green_exit], [0, 1, 0]);
  assert.match(record.red_output, /AssertionError/);
  assert.ok(record.command.includes('not slow'));
});

test('missing pytest blocks before any production edit', (t) => {
  const root = sources(t);
  const original = fs.readFileSync(path.join(root, edit.file));
  let calls = 0;
  assert.throws(() => verify(root, () => {
    calls += 1;
    return { status: 1, stderr: 'No module named pytest' };
  }));
  assert.equal(calls, 1);
  assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
});

test('a failed restored run cannot produce successful mutation evidence', (t) => {
  const root = sources(t);
  const original = fs.readFileSync(path.join(root, edit.file));
  let calls = 0;
  assert.throws(() => verify(root, () => {
    calls += 1;
    return verdict(calls !== 1);
  }));
  assert.equal(calls, 3);
  assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
});

for (const [label, file] of [
  ['production', edit.file],
  ['test', 'tests/test_coverage_round_1138.py'],
]) {
  test(`rejects ${label} source changes during the baseline before mutating production`, (t) => {
    const root = sources(t);
    let calls = 0;
    assert.throws(() => verify(root, () => {
      calls += 1;
      fs.appendFileSync(path.join(root, file), '# unexpected baseline change\n');
      return verdict();
    }), new RegExp(`baseline changed ${label} source`));
    assert.equal(calls, 1);
    assert.ok(fs.readFileSync(path.join(root, edit.file), 'utf8').includes(edit.before));
  });

  test(`rejects ${label} source changes during the deliberate break and restores production`, (t) => {
    const root = sources(t);
    const original = fs.readFileSync(path.join(root, edit.file));
    let calls = 0;
    assert.throws(() => verify(root, () => {
      calls += 1;
      if (calls === 2) fs.appendFileSync(path.join(root, file), '# unexpected mutation change\n');
      return verdict(calls === 2);
    }), new RegExp(`mutation run changed ${label} source`));
    assert.equal(calls, 2);
    assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
  });
}

for (const [label, red] of [
  ['timeout', () => ({ status: null, error: new Error('ETIMEDOUT') })],
  ['runner exception', () => { throw new Error('interrupted runner'); }],
  ['surviving mutation', () => verdict()],
  ['unrelated assertion failure', () => ({ ...verdict(true),
    stdout: verdict(true).stdout.replace("{'broken': True}", "{'unrelated': True}") })],
]) {
  test(`restores production bytes after ${label}`, (t) => {
    const root = sources(t);
    const original = fs.readFileSync(path.join(root, edit.file));
    let calls = 0;
    assert.throws(() => verify(root, () => (++calls === 1 ? verdict() : red())));
    assert.equal(calls, 2);
    assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
  });
}

for (const [label, result] of [
  ['missing pytest', { status: 1, stderr: 'No module named pytest' }],
  ['collection error', { status: 2, stdout: '1 error during collection' }],
  ['no tests', { status: 0, stdout: 'no tests ran' }],
  ['wrong test', { ...verdict(true), stdout: verdict(true).stdout.replaceAll(node, 'other::test') }],
  ['duplicate results', { ...verdict(true), stdout: verdict(true).stdout.repeat(2) }],
  ['skipped tests', { ...verdict(true), stdout: verdict(true).stdout + '1 skipped' }],
  ['extra test from another module', { ...verdict(true),
    stdout: verdict(true).stdout.replace('1 failed in',
      'tests/test_other.py::test_other PASSED [100%]\n1 failed, 1 passed in') }],
  ['extra result only in summary', { ...verdict(true),
    stdout: verdict(true).stdout.replace('1 failed in', '1 failed, 1 passed in') }],
  ['missing summary', { ...verdict(true),
    stdout: verdict(true).stdout.replace('1 failed in 0.1s\n', '') }],
]) {
  test(`rejects ${label} as deliberate-break evidence`, () => {
    assert.throws(() => requireVerdict(result, true));
  });
}

test('accepts pytest warning counts in a single-test summary', () => {
  for (const failed of [false, true]) {
    for (const warnings of ['', ', 1 warning', ', 2 warnings']) {
      const result = verdict(failed);
      result.stdout = result.stdout.replace(' in 0.1s\n', `${warnings} in 70.00s (0:01:10)\n`);
      result.stdout = result.stdout.replace(/^(1 (?:failed|passed).*s.*)$/m, '===== $1 =====');
      assert.equal(requireVerdict(result, failed), result.stdout);
    }
  }
});

test('rejects an extra result in the baseline or restored summary', () => {
  const result = verdict();
  result.stdout = result.stdout.replace('1 passed in', '2 passed in');
  assert.throws(() => requireVerdict(result, false));
});
