'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { experiments, verify } = require('../scripts/verify_issue_1138_artifacts');

function inputs(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'issue-1138-artifacts-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const originals = new Map();
  for (const { file, field } of experiments) {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    // Deliberately noncanonical formatting proves that restoration preserves bytes.
    const bytes = Buffer.from(`{ "${field}": 296, "preserved": [1, 2] }\n`);
    fs.writeFileSync(path.join(root, file), bytes);
    originals.set(file, bytes);
  }
  fs.mkdirSync(path.join(root, 'tests'));
  fs.writeFileSync(path.join(root, 'tests/test_coverage_round_1138.py'), '# private input\n');
  return { root, originals };
}

function verdict(testName, failed = false, mismatch = '296 == 297') {
  return {
    status: failed ? 1 : 0,
    stdout: `tests/test_coverage_round_1138.py::${testName} ${failed ? 'FAILED' : 'PASSED'} [100%]\n`
      + (failed ? `E   assert ${mismatch}\n` : '')
      + `1 ${failed ? 'failed' : 'passed'} in 0.1s\n`,
  };
}

function assertRestored(root, originals) {
  for (const [file, bytes] of originals) {
    assert.deepEqual(fs.readFileSync(path.join(root, file)), bytes);
  }
}

test('independently breaks each gain lock and restores both original records', (t) => {
  const { root, originals } = inputs(t);
  let calls = 0;
  const results = verify(root, (cwd, testName) => {
    assert.equal(cwd, root);
    const index = Math.floor(calls / 3);
    const failed = calls % 3 === 1;
    assert.equal(testName, experiments[index].test);
    for (let i = 0; i < experiments.length; i += 1) {
      const { file, field } = experiments[i];
      const bytes = fs.readFileSync(path.join(root, file));
      const record = JSON.parse(bytes);
      assert.equal(record[field], failed && i === index ? 297 : 296);
      assert.deepEqual(record.preserved, [1, 2]);
      if (!failed || i !== index) assert.deepEqual(bytes, originals.get(file));
    }
    calls += 1;
    return verdict(testName, failed, index === 0 ? '296 == 297' : '297 == 296');
  });
  assert.equal(calls, 6);
  assert.equal(results.length, 2);
  for (let i = 0; i < results.length; i += 1) {
    const result = results[i];
    assert.equal(result.test, experiments[i].test);
    assert.equal(result.field, experiments[i].field);
    assert.equal(result.before, 296);
    assert.equal(result.broken, 297);
    assert.equal(result.record_sha256, result.restored_record_sha256);
    assert.notEqual(result.record_sha256, result.broken_record_sha256);
    assert.match(result.test_sha256, /^[a-f0-9]{64}$/);
    assert.deepEqual([result.baseline_exit, result.red_exit, result.green_exit], [0, 1, 0]);
    assert.ok(result.command.includes('not slow'));
  }
  assertRestored(root, originals);
});

for (const index of [0, 1]) {
  for (const [label, brokenResult] of [
    ['missing pytest', () => ({ status: 1, stderr: 'No module named pytest' })],
    ['collection error', () => ({ status: 2, stdout: '1 error during collection' })],
    ['timeout', () => ({ status: null, error: new Error('ETIMEDOUT') })],
    ['runner exception', () => { throw new Error('interrupted runner'); }],
    ['surviving corruption', (name) => verdict(name)],
    ['unrelated assertion', (name) => verdict(name, true, '0 == 1')],
    ['wrong node', () => verdict('test_other', true)],
    ['extra test from another module', (name, mismatch) => ({ ...verdict(name, true, mismatch),
      stdout: verdict(name, true, mismatch).stdout.replace('1 failed in',
        'tests/test_other.py::test_other PASSED [100%]\n1 failed, 1 passed in') })],
    ['extra result only in summary', (name, mismatch) => ({ ...verdict(name, true, mismatch),
      stdout: verdict(name, true, mismatch).stdout.replace('1 failed in', '1 failed, 1 passed in') })],
    ['skip', (name) => ({ ...verdict(name, true),
      stdout: verdict(name, true).stdout + '1 skipped\n' })],
  ]) {
    test(`restores all records after ${label} in experiment ${index + 1}`, (t) => {
      const { root, originals } = inputs(t);
      let calls = 0;
      assert.throws(() => verify(root, (_cwd, name) => {
        const call = calls++;
        if (call === index * 3 + 1) {
          return brokenResult(name, index === 0 ? '296 == 297' : '297 == 296');
        }
        return verdict(name, call % 3 === 1, '296 == 297');
      }));
      assert.equal(calls, index * 3 + 2);
      assertRestored(root, originals);
    });
  }
}

test('a failed baseline stops before corrupting any record', (t) => {
  const { root, originals } = inputs(t);
  let calls = 0;
  assert.throws(() => verify(root, () => {
    calls += 1;
    assertRestored(root, originals);
    return { status: 1, stderr: 'No module named pytest' };
  }));
  assert.equal(calls, 1);
  assertRestored(root, originals);
});

test('rejects a failed restored run instead of recording success', (t) => {
  const { root, originals } = inputs(t);
  let calls = 0;
  assert.throws(() => verify(root, (_cwd, name) => {
    calls += 1;
    if (calls === 3) {
      assertRestored(root, originals);
      return verdict(name, true);
    }
    return verdict(name, calls === 2);
  }));
  assert.equal(calls, 3);
  assertRestored(root, originals);
});

test('rejects an unexpected starting gain without invoking pytest', (t) => {
  const { root } = inputs(t);
  fs.writeFileSync(path.join(root, experiments[0].file), '{"gain": 297}\n');
  assert.throws(() => verify(root, () => assert.fail('must not run')), /unexpected starting value/);
});

for (const index of [0, 1]) {
  for (const phase of ['baseline', 'mutation', 'restored']) {
    for (const target of ['active record', 'other record', 'test source']) {
      test(`rejects ${target} changes during ${phase} in experiment ${index + 1}`, (t) => {
        const { root, originals } = inputs(t);
        const phaseCall = ['baseline', 'mutation', 'restored'].indexOf(phase);
        const file = target === 'test source' ? 'tests/test_coverage_round_1138.py'
          : experiments[target === 'active record' ? index : 1 - index].file;
        let calls = 0;
        assert.throws(() => verify(root, (_cwd, name) => {
          const call = calls++;
          if (call === index * 3 + phaseCall) {
            fs.appendFileSync(path.join(root, file), '\n');
          }
          return verdict(name, call % 3 === 1,
            Math.floor(call / 3) === 0 ? '296 == 297' : '297 == 296');
        }), /coverage record (?:changed|was not restored)|test source changed/);
        assert.equal(calls, index * 3 + phaseCall + 1);
        // The intended corruption is always restored, including when the runner
        // rewrites that record during the deliberately failing test.
        if (phase === 'mutation') {
          const active = experiments[index].file;
          assert.deepEqual(fs.readFileSync(path.join(root, active)), originals.get(active));
        }
      });
    }
  }
}
