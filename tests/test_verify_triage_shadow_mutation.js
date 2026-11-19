'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { edit, node, requireVerdict, verify } = require('../scripts/verify_triage_shadow_mutation');

// These simulated transcripts test the runner; they are not pytest acceptance evidence.
function verdict(failed = false) {
  return {
    status: failed ? 1 : 0,
    stdout: `${node} ${failed ? 'FAILED' : 'PASSED'} [100%]\n`
      + (failed ? "E   KeyError: 'pr_reads'\n" : '')
      + `1 ${failed ? 'failed' : 'passed'} in 0.1s\n`,
  };
}

function sources(t, newline = '\n', invocations = 1) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'triage-mutation-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'src'));
  fs.mkdirSync(path.join(root, 'tests'));
  const original = Buffer.from(`# preserved input${newline}`
    + edit.before.replaceAll('\n', newline).repeat(invocations));
  fs.writeFileSync(path.join(root, edit.file), original);
  fs.writeFileSync(path.join(root, 'tests/test_triage_shadow.py'), '# preserved test\n');
  return { root, original };
}

for (const newline of ['\n', '\r\n']) {
  test(`removes only the actual invocation and restores ${JSON.stringify(newline)} bytes`, (t) => {
    const { root, original } = sources(t, newline);
    let calls = 0;
    const caches = [];
    const python = path.resolve(os.tmpdir(), 'selected-python');
    const record = verify(root, python, (cwd, interpreter, cache) => {
      assert.equal(cwd, root);
      assert.equal(interpreter, python);
      assert.ok(fs.existsSync(cache));
      caches.push(cache);
      calls += 1;
      const expected = calls === 2
        ? original.toString().replace(edit.before.replaceAll('\n', newline),
          edit.after.replaceAll('\n', newline)) : original.toString();
      assert.equal(fs.readFileSync(path.join(root, edit.file), 'utf8'), expected);
      return verdict(calls === 2);
    });
    assert.equal(calls, 3);
    assert.equal(new Set(caches).size, 1);
    assert.ok(!fs.existsSync(caches[0]));
    assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
    assert.equal(record.source_sha256, record.restored_source_sha256);
    assert.notEqual(record.source_sha256, record.broken_source_sha256);
    assert.deepEqual([record.baseline_exit, record.red_exit, record.green_exit], [0, 1, 0]);
    assert.equal(record.command[0], python);
    assert.ok(record.command.includes(node));
    assert.ok(record.command.includes('not slow'));
    assert.match(record.red_output, /KeyError: 'pr_reads'/);
  });
}

for (const [label, bad] of [
  ['missing pytest', () => ({ status: 1, stderr: 'No module named pytest' })],
  ['collection failure', () => ({ status: 2, stdout: '1 error during collection' })],
  ['timeout', () => ({ status: null, error: new Error('ETIMEDOUT') })],
  ['signal', () => ({ ...verdict(true), signal: 'SIGTERM' })],
  ['runner exception', () => { throw new Error('runner interrupted'); }],
  ['surviving mutation', () => verdict()],
  ['unrelated failure', () => ({ ...verdict(true),
    stdout: verdict(true).stdout.replace("KeyError: 'pr_reads'", 'AssertionError: unrelated') })],
]) {
  test(`rejects ${label} and restores production after removal`, (t) => {
    const { root, original } = sources(t);
    let calls = 0;
    let cache;
    assert.throws(() => verify(root, process.execPath, (_root, _python, prefix) => {
      cache = prefix;
      return ++calls === 1 ? verdict() : bad();
    }));
    assert.equal(calls, 2);
    assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
    assert.ok(!fs.existsSync(cache));
  });
}

test('missing pytest stops before editing production', (t) => {
  const { root, original } = sources(t);
  let calls = 0;
  assert.throws(() => verify(root, process.execPath, () => {
    calls += 1;
    return { status: 1, stderr: 'No module named pytest' };
  }));
  assert.equal(calls, 1);
  assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
});

test('a failed restored run cannot emit a receipt', (t) => {
  const { root, original } = sources(t);
  let calls = 0;
  assert.throws(() => verify(root, process.execPath, () => verdict(++calls !== 1)));
  assert.equal(calls, 3);
  assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
});

for (const invocations of [0, 2]) {
  test(`rejects ${invocations} invocation anchors before starting pytest`, (t) => {
    const { root, original } = sources(t, '\n', invocations);
    assert.throws(() => verify(root, process.execPath, () => assert.fail('must not run')),
      /invocation must occur exactly once/);
    assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
  });
}

for (const phase of [1, 2, 3]) {
  test(`restores deleted production source after pytest call ${phase} throws`, (t) => {
    const { root, original } = sources(t);
    let calls = 0;
    assert.throws(() => verify(root, process.execPath, () => {
      calls += 1;
      if (calls === phase) {
        fs.unlinkSync(path.join(root, edit.file));
        throw new Error('runner interrupted after removing source');
      }
      return verdict(calls === 2);
    }), /runner interrupted/);
    assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
  });
  for (const file of [edit.file, 'tests/test_triage_shadow.py']) {
    test(`rejects changed ${file} during pytest call ${phase}`, (t) => {
      const { root, original } = sources(t);
      let calls = 0;
      assert.throws(() => verify(root, process.execPath, () => {
        calls += 1;
        if (calls === phase) fs.appendFileSync(path.join(root, file), '# unexpected\n');
        return verdict(calls === 2);
      }), /source changed during pytest/);
      assert.equal(calls, phase);
      assert.deepEqual(fs.readFileSync(path.join(root, edit.file)), original);
    });
  }
}

test('requires an explicitly selected absolute interpreter', (t) => {
  const { root } = sources(t);
  for (const python of [undefined, '', 'python3']) {
    assert.throws(() => verify(root, python, () => assert.fail('must not run')),
      /repository-selected interpreter/);
  }
});

for (const output of [
  verdict(true).stdout.replaceAll(node, 'tests/test_other.py::test_other'),
  verdict(true).stdout.replace('1 failed in', '1 failed, 1 passed in'),
  verdict(true).stdout + '1 skipped\n',
]) {
  test('rejects a wrong test or additional pytest results', () => {
    assert.throws(() => requireVerdict({ status: 1, stdout: output }, true));
  });
}
