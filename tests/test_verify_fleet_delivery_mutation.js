'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { controls, mutation, requireVerdict, runIsolated, verify } = require('../scripts/verify_fleet_delivery_mutation');

test('real fleet test detects the mutation and passes again after restoration', async () => {
  const repo = path.resolve(__dirname, '..');
  const source = fs.readFileSync(path.join(repo, 'src/switch_review.py'));
  const result = await runIsolated(['--unittest'], process.env.PYTHON || 'python3');
  assert.match(result, /unittest: baseline passed; fail-open mutation detected; restored test passed/);
  assert.deepEqual(fs.readFileSync(path.join(repo, 'src/switch_review.py')), source);
});

test('every real reader rejects a swallowed error and passes after restoration', async () => {
  const repo = path.resolve(__dirname, '..');
  const source = fs.readFileSync(path.join(repo, 'src/switch_review.py'));
  const result = await runIsolated(['--all-readers', '--unittest'], process.env.PYTHON || 'python3');
  const keys = ['gate_cancellations', 'merge_to_close', 'oldest_canary', 'opener_cap',
    'released_flow', 'closing_issue_refs'];
  assert.deepEqual(Object.keys(controls), keys);
  assert.deepEqual(result.split('\n'), keys.map((key) =>
    `${key}: unittest: baseline passed; fail-open mutation detected; restored test passed`));
  assert.deepEqual(fs.readFileSync(path.join(repo, 'src/switch_review.py')), source);
});

test('all-readers evidence must name the selected test', () => {
  for (const control of Object.values(controls)) {
    for (const runner of ['pytest', 'unittest']) {
      assert.throws(() => requireVerdict({
        status: 1, signal: null,
        stdout: "tests/test_unrelated.py::test_wrong FAILED\nAssertionError: '0%' is not None\n1 failed",
      }, runner, true, control));
    }
  }
});

test('structured-link mutation evidence must detect a partial numeric PR measure', () => {
  const control = controls.closing_issue_refs;
  assert.ok(control, 'all-readers must mutate the structured-link reader itself');
  const node = `tests/test_switch_review_fleet.py::FleetDeliveryTests::${control.testName}`;
  const output = `${node} FAILED [100%]\n`
    + "AssertionError: 'median 15 min (2 PR/issue pairs; 1 still open)' is not None\n1 failed";
  requireVerdict({ status: 1, signal: null, stdout: output }, 'pytest', true, control);
  assert.throws(() => requireVerdict({
    status: 1, signal: null, stdout: output.replace(
      "'median 15 min (2 PR/issue pairs; 1 still open)' is not None",
      "'0%' is not None"),
  }, 'pytest', true, control));
});

test('unittest subtest evidence must contain only failures from the selected method', () => {
  const control = controls.oldest_canary;
  const name = `${control.testName} (test_switch_review_fleet.FleetDeliveryTests.${control.testName})`;
  const output = `${name} ... \n`
    + `FAIL: ${name} (endpoint='contents')\n`
    + "AssertionError: '0%' is not None\n"
    + `FAIL: ${name} (endpoint='pulls')\n`
    + "AssertionError: '0%' is not None\nRan 1 test in 0.01s\nFAILED (failures=2)\n";
  const result = { status: 1, signal: null, stdout: output };
  requireVerdict(result, 'unittest', true, control);
  for (const broken of [
    output.replace('failures=2', 'failures=1'),
    output.replace(`FAIL: ${name}`, 'FAIL: test_wrong (test_unrelated.Test.test_wrong)'),
    output.replace('Ran 1 test', 'Ran 2 tests'),
    output.replace('failures=2', 'failures=2, errors=1'),
    output + '\nERROR: test_wrong\n',
  ]) {
    assert.throws(() => requireVerdict({ ...result, stdout: broken }, 'unittest', true, control));
  }
});

test('reader options reject misspellings and duplicates', async () => {
  for (const args of [['--all-reader'], ['--unittest', '--unittest'], ['--all-readers', '--all-readers']]) {
    await assert.rejects(runIsolated(args), /usage:/);
  }
});

test('mutation verification requires explicit interpreter selection', async (t) => {
  const previous = process.env.PYTHON;
  delete process.env.PYTHON;
  t.after(() => {
    if (previous === undefined) delete process.env.PYTHON;
    else process.env.PYTHON = previous;
  });
  for (const python of [undefined, '', '   ', 42]) {
    await assert.rejects(runIsolated(['--all-readers'], python),
      /set PYTHON to the repository-selected interpreter/);
    await assert.rejects(verify('/unused', 'pytest', python),
      /set PYTHON to the repository-selected interpreter/);
  }
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

test('a blocked mutation run restores every reader source byte', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-mutation-restoration-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'src'));
  const file = path.join(root, 'src/switch_review.py');
  for (const control of Object.values(controls)) {
    const original = `${mutation(control).anchor}    pass\n`;
    fs.writeFileSync(file, original);
    let calls = 0;
    const run = (directory, runner, python, selected) => {
      assert.equal(selected, control);
      calls += 1;
      if (calls > 1) {
        assert.match(fs.readFileSync(file, 'utf8'), /deliberate fail-open control/);
        throw new Error('test runner unavailable');
      }
      return {
        status: 0, signal: null,
        stdout: `tests/test_switch_review_fleet.py::FleetDeliveryTests::${control.testName} PASSED [100%]\n1 passed`,
      };
    };
    await assert.rejects(verify(root, 'pytest', 'python3', run, control), /test runner unavailable/);
    assert.equal(fs.readFileSync(file, 'utf8'), original);
    assert.equal(calls, 2);
  }
});

test('CRLF reader sources detect mutations and restore their original bytes', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-mutation-crlf-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'src'));
  const file = path.join(root, 'src/switch_review.py');
  for (const control of Object.values(controls)) {
    const original = Buffer.from(`${mutation(control).anchor}    pass\n# retained — evidence\n`
      .replaceAll('\n', '\r\n'));
    fs.writeFileSync(file, original);
    let calls = 0;
    const run = (directory, runner, python, selected) => {
      assert.equal(selected, control);
      calls += 1;
      const failed = calls === 2;
      const bytes = fs.readFileSync(file);
      if (failed) {
        const source = bytes.toString('utf8');
        assert.match(source, /deliberate fail-open control/);
        assert.doesNotMatch(source.replaceAll('\r\n', ''), /[\r\n]/,
          'inserting a mutation must preserve CRLF line endings');
      } else {
        assert.deepEqual(bytes, original);
      }
      const assertion = control === controls.closing_issue_refs
        ? "AssertionError: 'opened 2/day' is not None"
        : "AssertionError: '0%' is not None";
      return {
        status: failed ? 1 : 0, signal: null,
        stdout: `tests/test_switch_review_fleet.py::FleetDeliveryTests::${control.testName} `
          + `${failed ? 'FAILED' : 'PASSED'} [100%]\n`
          + (failed ? `${assertion}\n1 failed` : '1 passed'),
      };
    };
    await verify(root, 'pytest', '/selected/python', run, control);
    assert.equal(calls, 3);
    assert.deepEqual(fs.readFileSync(file), original);
    calls = 0;
    await assert.rejects(verify(root, 'pytest', '/selected/python', (...args) => {
      const result = run(...args);
      if (calls === 2) throw new Error('runner interrupted');
      return result;
    }, control), /runner interrupted/);
    assert.deepEqual(fs.readFileSync(file), original);
  }
});
