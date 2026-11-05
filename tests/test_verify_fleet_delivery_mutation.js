'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { anchor, requireVerdict, runIsolated, verify } = require('../scripts/verify_fleet_delivery_mutation');

test('real fleet test detects the mutation and passes again after restoration', async () => {
  const repo = path.resolve(__dirname, '..');
  const source = fs.readFileSync(path.join(repo, 'src/switch_review.py'));
  const result = await runIsolated(['--unittest'], process.env.PYTHON || 'python3');
  assert.match(result, /unittest: baseline passed; fail-open mutation detected; restored test passed/);
  assert.deepEqual(fs.readFileSync(path.join(repo, 'src/switch_review.py')), source);
});

test('missing tools, skipped tests and unrelated failures cannot count as mutation evidence', () => {
  for (const runner of ['pytest', 'unittest']) {
    for (const result of [
      { status: 1, signal: null, stderr: 'No module named pytest' },
      { status: 1, signal: null, stdout: '1 failed\nAssertionError: unrelated failure' },
      { status: 0, signal: null, stdout: '1 skipped' },
      { status: null, signal: 'SIGTERM', error: new Error('timeout') },
    ]) {
      assert.throws(() => requireVerdict(result, runner, true));
    }
  }
});

test('pytest evidence must report only the one selected test', () => {
  const node = 'tests/test_switch_review_fleet.py::FleetDeliveryTests::test_failed_gate_read_is_not_a_number';
  for (const failed of [false, true]) {
    const status = failed ? 1 : 0;
    const verdict = failed ? 'FAILED' : 'PASSED';
    const count = failed ? '1 failed' : '1 passed';
    const assertion = failed ? "\nAssertionError: '0%' is not None" : '';
    const output = `${node} ${verdict} [100%]${assertion}\n${count}`;
    requireVerdict({ status, signal: null, stdout: output }, 'pytest', failed);
    // A matching assertion in a larger run is insufficient proof of the selected control.
    for (const extra of ['1 passed', '1 failed', '2 passed', '2 failed']) {
      assert.throws(() => requireVerdict({
        status, signal: null, stdout: `${output}, ${extra}`,
      }, 'pytest', failed), `accepted ${count}, ${extra}`);
    }
  }
});

test('a blocked mutation run restores the original source bytes', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-mutation-restoration-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'src'));
  const file = path.join(root, 'src/switch_review.py');
  const original = `${anchor}    pass\n`;
  fs.writeFileSync(file, original);
  let calls = 0;
  const run = () => {
    calls += 1;
    if (calls > 1) {
      assert.match(fs.readFileSync(file, 'utf8'), /deliberate fail-open control/);
      throw new Error('test runner unavailable');
    }
    return {
      status: 0, signal: null,
      stdout: 'tests/test_switch_review_fleet.py::FleetDeliveryTests::test_failed_gate_read_is_not_a_number PASSED [100%]\n1 passed',
    };
  };
  await assert.rejects(verify(root, 'pytest', 'python3', run), /test runner unavailable/);
  assert.equal(fs.readFileSync(file, 'utf8'), original);
  assert.equal(calls, 2);
});
