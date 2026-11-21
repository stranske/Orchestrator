'use strict';

const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { edit, node, pytestCommand, requireVerdict, runPytest, verify, verifyAtHead } =
  require('../scripts/verify_issue_1138_mutation');

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

test('anchors a relative Python executable to the invocation directory', (t) => {
  const previous = process.env.PYTHON;
  t.after(() => {
    if (previous === undefined) delete process.env.PYTHON;
    else process.env.PYTHON = previous;
  });
  const relative = path.join('.venv', 'bin', 'python');
  process.env.PYTHON = relative;
  assert.equal(pytestCommand()[0], path.resolve(relative));
  process.env.PYTHON = 'python3.14';
  assert.equal(pytestCommand()[0], 'python3.14');
});

test('keeps host pytest options and plugins out of the mutation run', (t) => {
  const root = sources(t);
  const interpreter = path.join(root, 'inspect-pytest-env');
  const envRecord = path.join(root, 'pytest-env.json');
  const inspector = '#!/usr/bin/env node\n'
    + 'const { PYTEST_ADDOPTS, PYTEST_PLUGINS, PYTEST_DISABLE_PLUGIN_AUTOLOAD } = process.env;\n'
    + "require('node:fs').writeFileSync('pytest-env.json', "
    + 'JSON.stringify({ PYTEST_ADDOPTS, PYTEST_PLUGINS, PYTEST_DISABLE_PLUGIN_AUTOLOAD }));\n';
  fs.writeFileSync(interpreter, inspector, { mode: 0o755 });
  const hostEnv = {
    PYTHON: interpreter,
    PYTEST_ADDOPTS: '--tb=no',
    PYTEST_PLUGINS: 'local_plugin',
    PYTEST_DISABLE_PLUGIN_AUTOLOAD: '0',
  };
  for (const [key, value] of Object.entries(hostEnv)) {
    const previous = process.env[key];
    t.after(() => {
      if (previous === undefined) delete process.env[key];
      else process.env[key] = previous;
    });
    process.env[key] = value;
  }
  const result = runPytest(root, node);
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(fs.readFileSync(envRecord, 'utf8')),
    { PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1' });
});

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

test('runs the committed mutation input in a disposable tree and removes it on failure', () => {
  const root = path.resolve(__dirname, '..');
  const source = path.join(root, edit.file);
  const original = fs.readFileSync(source);
  let checkout;
  let calls = 0;
  assert.throws(() => verifyAtHead(root, 'HEAD', (cwd) => {
    calls += 1;
    checkout = cwd;
    assert.notEqual(cwd, root);
    assert.deepEqual(fs.readFileSync(source), original);
    const input = fs.readFileSync(path.join(cwd, edit.file), 'utf8');
    assert.ok(input.includes(calls === 2 ? edit.after : edit.before));
    if (calls === 2) throw new Error('interrupted mutation run');
    return verdict();
  }), /interrupted mutation run/);
  assert.equal(calls, 2);
  assert.equal(fs.existsSync(checkout), false);
  assert.deepEqual(fs.readFileSync(source), original);
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

for (const phase of ['baseline', 'restored']) {
  for (const outcome of ['pass', 'failure', 'exception']) {
    test(`restores production rewritten during ${phase} with runner ${outcome}`, (t) => {
      const root = sources(t);
      const source = path.join(root, edit.file);
      const original = fs.readFileSync(source);
      const phaseCall = phase === 'baseline' ? 1 : 3;
      let calls = 0;
      assert.throws(() => verify(root, () => {
        calls += 1;
        if (calls === phaseCall) {
          fs.writeFileSync(source, original.toString().replace(edit.before, edit.after));
          if (outcome === 'exception') throw new Error('runner rewrote production');
          return verdict(outcome === 'failure');
        }
        return verdict(calls === 2);
      }));
      assert.equal(calls, phaseCall);
      assert.deepEqual(fs.readFileSync(source), original);
    });
  }
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

test('rejects the expected assertion when pytest reports a different failure', () => {
  const result = verdict(true);
  const expected = "E   AssertionError: assert {'broken': True} == {}\n";
  result.stdout = result.stdout.replace(expected,
    'E   AssertionError: assert False\n'
    + '----------------------------- Captured stdout call -----------------------------\n'
    + expected);
  result.stderr = expected;
  assert.throws(() => requireVerdict(result, true), /assert False/);
});

test('rejects an extra result in the baseline or restored summary', () => {
  const result = verdict();
  result.stdout = result.stdout.replace('1 passed in', '2 passed in');
  assert.throws(() => requireVerdict(result, false));
});

test('accepts the recorded real corrected-test mutation transcripts', () => {
  const evidence = JSON.parse(fs.readFileSync(path.join(__dirname,
    '../docs/verification/issue-1138/mutation-proof-pass3.json'), 'utf8'));
  const cases = evidence.followup_runs.flatMap((run) => run.cases)
    .filter((record) => `${record.file}` === edit.file && record.test === node.split('::')[1]);
  assert.ok(cases.length > 0, 'missing real corrected-test mutation evidence');
  for (const record of cases) {
    for (const phase of ['baseline', 'red', 'green']) {
      const result = { status: record[`${phase}_exit`], stdout: record[`${phase}_output`] };
      assert.equal(requireVerdict(result, phase === 'red'), result.stdout);
      // The real assertion and node must not make the opposite verdict valid.
      assert.throws(() => requireVerdict(result, phase !== 'red'));
    }
  }
});
