'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { verify, runCommand } = require('../scripts/verify_redirect_merged_revision');

test('real asynchronous child exits retain output, failure status and spawn errors', async () => {
  const options = { encoding: 'utf8', timeout: 5000, maxBuffer: 4096 };
  const success = await runCommand('python3', ['--version'], options);
  assert.equal(success.status, 0);
  assert.equal(success.signal, null);
  assert.equal(success.error, undefined);
  assert.match(success.stdout.trim(), /^Python 3\./);

  const failed = await runCommand('python3', ['--invalid-redirect-verifier-option'], options);
  assert.notEqual(failed.status, 0);
  assert.equal(failed.signal, null);
  assert.equal(failed.error, undefined);
  assert.match(failed.stderr, /unknown option/i);

  const absent = await runCommand('/nonexistent/redirect-verifier-python', [], options);
  assert.equal(absent.status, null);
  assert.equal(absent.error.code, 'ENOENT');
});

test('real asynchronous children remain bounded by timeout and output limit', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-process-control-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const script = path.join(root, 'control.py');
  fs.writeFileSync(script, 'import sys, time\nsys.stdout.write("x" * 8192)\nsys.stdout.flush()\ntime.sleep(30)\n');
  const overflow = await runCommand('python3', [script], {
    encoding: 'utf8', timeout: 5000, maxBuffer: 1024,
  });
  assert.equal(overflow.status, null);
  assert.equal(overflow.error.code, 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER');
  assert.equal(overflow.stdout.length, 1024);

  const expired = await runCommand('python3', [script], {
    encoding: 'utf8', timeout: 100, maxBuffer: 16384,
  });
  assert.equal(expired.status, null);
  assert.equal(expired.error.code, 'ETIMEDOUT');
  assert.equal(expired.signal, 'SIGTERM');
});

test('real asynchronous stdin preserves archive bytes without text decoding', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-archive-control-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const script = path.join(root, 'control.py');
  fs.writeFileSync(script, 'import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n');
  const input = Buffer.from(Array.from({ length: 256 }, (_, i) => i));
  const result = await runCommand('python3', [script], {
    input, timeout: 5000, maxBuffer: 4096,
  });
  assert.equal(result.status, 0);
  assert.equal(result.error, undefined);
  assert.deepEqual(result.stdout, input);
});

function repository(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-revision-control-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const git = (...args) => {
    const result = spawnSync('git', ['-C', root, ...args], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  };
  git('init', '-q');
  fs.mkdirSync(path.join(root, 'src'));
  fs.mkdirSync(path.join(root, 'tests'));
  fs.writeFileSync(path.join(root, 'src/redirect_apply.py'), '# manufactured control\n');
  fs.writeFileSync(path.join(root, 'tests/retained.txt'), 'committed fixture bytes\n');
  fs.writeFileSync(path.join(root, 'pyproject.toml'), '# manufactured control\n');
  git('add', '.');
  const commit = () => {
    git('-c', 'user.name=Control', '-c', 'user.email=control@example.invalid',
      'commit', '-qm', 'manufactured verifier control');
    return git('rev-parse', 'HEAD');
  };
  const revision = commit();
  git('update-ref', 'refs/remotes/origin/main', revision);
  return { root, revision, git, commit };
}

// Git ancestry, archive extraction and cleanup are real. Only Python results are
// manufactured controls; they never establish pytest, replay or historical evidence.
function pythonControl(change = () => {}) {
  const calls = [];
  function run(command, args, options) {
    if (command !== '/control/python') return runCommand(command, args, options);
    calls.push({ args, options });
    const source = fs.readFileSync(path.join(options.cwd, 'tests/retained.txt'), 'utf8');
    assert.equal(source, 'committed fixture bytes\n');
    assert.equal(fs.existsSync(path.join(options.cwd, 'tests/untracked.txt')), false);
    assert.equal(options.env.ORCH_REDIRECT_APPLY_BOOTSTRAP, '0');
    assert.equal(options.env.ORCH_CAPABILITY_HEARTBEATS, '0');
    assert.equal(options.env.PYTEST_ADDOPTS, undefined);
    assert.equal(options.env.PYTHONPATH, undefined);
    assert.ok(options.env.ORCH_STATE_DIR.startsWith(path.dirname(options.cwd)));
    let stdout;
    if (args.includes('-c')) stdout = '{"executable":"/control/python","version":"control"}\n';
    else if (args.includes('--replay-stalls')) stdout = 'authorized 12/12 with named agents (dry-run)\n';
    else {
      const nodes = args.filter((arg) => arg.includes('::'));
      const executed = nodes.length ? nodes : [
        'tests/test_redirect_chain_gate.py::test_control',
        'tests/test_redirect_apply_prescreen.py::test_control',
      ];
      stdout = executed.map((node) => `${node} PASSED [100%]\n`).join('')
        + (nodes.length ? '2 passed in 0.01s\n' : '40 passed in 0.02s\n');
    }
    const result = { status: 0, stdout, stderr: '' };
    change(result, args);
    return result;
  }
  return { run, calls };
}

test('runs all required checks from merged bytes with isolated state and current dates', async (t) => {
  const s = repository(t);
  // Dirty source and untracked fixtures must not alter the identified revision.
  fs.writeFileSync(path.join(s.root, 'tests/retained.txt'), 'dirty bytes\n');
  fs.writeFileSync(path.join(s.root, 'tests/untracked.txt'), 'untracked bytes\n');
  const control = pythonControl();
  const started = Date.now();
  const result = await verify({ repository: s.root, revision: s.revision, python: '/control/python', run: control.run });
  assert.equal(result.status, 'verified');
  assert.equal(result.merged_main_sha, s.revision);
  assert.match(result.archive_sha256, /^[a-f0-9]{64}$/);
  assert.equal(result.authorized, 12);
  assert.equal(result.historical_replay_receipt, 'not_established');
  assert.equal(result.operational_acceptance, 'not_established');
  assert.ok(Date.parse(result.observed_at) >= started);
  assert.deepEqual(result.executions.map((row) => row.name),
    ['interpreter', 'named_tests', 'focused_files', 'offline_replay']);
  for (const row of result.executions.filter((row) => row.name.includes('test') || row.name === 'focused_files')) {
    assert.deepEqual(row.argv.slice(-3), ['-v', '-m', 'not slow']);
  }
  assert.match(result.executions[1].argv.join(' '), /::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine/);
  assert.match(result.executions[1].argv.join(' '), /::test_redirect_apply_daily_cap_refuses_second_apply/);
  assert.ok(result.executions[2].argv.includes('tests/test_redirect_chain_gate.py'));
  assert.ok(result.executions[2].argv.includes('tests/test_redirect_apply_prescreen.py'));
  assert.equal(fs.existsSync(path.dirname(control.calls[0].options.cwd)), false);
  assert.equal(fs.readFileSync(path.join(s.root, 'tests/retained.txt'), 'utf8'), 'dirty bytes\n');
});

test('refuses unmerged commits, absent main, and option-shaped revisions before Python executes', async (t) => {
  const s = repository(t);
  const control = pythonControl();
  const check = (revision) => verify({ repository: s.root, revision, python: '/control/python', run: control.run });
  assert.equal((await check('--output=/tmp/unwanted')).reason, 'full_commit_sha_required');
  fs.writeFileSync(path.join(s.root, 'tests/unmerged.txt'), 'unmerged\n');
  s.git('add', '.');
  const unmerged = s.commit();
  assert.equal((await check(unmerged)).reason, 'revision_not_on_merged_main');
  s.git('update-ref', '-d', 'refs/remotes/origin/main');
  assert.equal((await check(s.revision)).reason, 'merged_main_reference_unavailable');
  assert.equal(control.calls.length, 0);
});

test('missing pytest stays blocked while offline replay still runs and cleanup completes', async (t) => {
  const s = repository(t);
  const control = pythonControl((result, args) => {
    if (args.includes('pytest')) Object.assign(result, {
      status: 1, stdout: '', stderr: '/control/python: No module named pytest\n',
    });
  });
  const result = await verify({ repository: s.root, revision: s.revision, python: '/control/python', run: control.run });
  assert.equal(result.status, 'blocked');
  assert.equal(result.authorized, 12);
  assert.deepEqual(result.executions.map((row) => row.exit_code), [0, 1, 1, 0]);
  assert.equal(fs.existsSync(path.dirname(control.calls[0].options.cwd)), false);
});

test('zero exits with skips, no passes, wrong replay totals, or insufficient authorization fail', async (t) => {
  const s = repository(t);
  for (const output of ['2 skipped in 0.01s\n', 'no tests ran in 0.01s\n', '2 passed, 1 skipped in 0.01s\n']) {
    const control = pythonControl((result, args) => {
      if (args.includes('pytest')) result.stdout = output;
    });
    assert.equal((await verify({ repository: s.root, revision: s.revision, python: '/control/python', run: control.run })).status, 'blocked');
  }
  for (const count of ['8/12', '9/11', '13/12', '9/12']) {
    const control = pythonControl((result, args) => {
      if (args.includes('--replay-stalls')) result.stdout = `authorized ${count} with named agents (dry-run)\n`;
    });
    const result = await verify({ repository: s.root, revision: s.revision, python: '/control/python', run: control.run });
    assert.equal(result.status, count === '9/12' ? 'verified' : 'blocked', count);
  }
});

test('a killed interpreter cannot become success and its process failure is retained', async (t) => {
  const s = repository(t);
  const control = pythonControl((result, args) => {
    if (args.includes('pytest')) Object.assign(result, {
      status: null, signal: 'SIGTERM', error: { code: 'ETIMEDOUT' },
    });
  });
  const result = await verify({ repository: s.root, revision: s.revision, python: '/control/python', run: control.run });
  assert.equal(result.status, 'blocked');
  assert.equal(result.executions[1].exit_code, null);
  assert.equal(result.executions[1].signal, 'SIGTERM');
  assert.equal(result.executions[1].error_code, 'ETIMEDOUT');
  assert.equal(fs.existsSync(path.dirname(control.calls[0].options.cwd)), false);
});

test('process errors prevent verification even if a child reports a zero exit', async (t) => {
  const s = repository(t);
  const control = pythonControl((result) => {
    result.error = { code: 'EPERM' };
  });
  const result = await verify({ repository: s.root, revision: s.revision, python: '/control/python', run: control.run });
  assert.equal(result.status, 'blocked');
  assert.equal(result.executions[0].exit_code, 0);
  assert.equal(result.executions[0].error_code, 'EPERM');
});

test('pass counts cannot substitute for executing both named nodes and both focused files', async (t) => {
  const s = repository(t);
  for (const mode of ['summary_only', 'wrong_named_node', 'one_focused_file']) {
    const control = pythonControl((result, args) => {
      if (!args.includes('pytest')) return;
      if (mode === 'summary_only') result.stdout = '2 passed in 0.01s\n';
      if (mode === 'wrong_named_node' && args.some((arg) => arg.includes('::'))) {
        result.stdout = result.stdout.replace('test_redirect_apply_daily_cap_refuses_second_apply', 'test_other');
      }
      if (mode === 'one_focused_file' && !args.some((arg) => arg.includes('::'))) {
        result.stdout = result.stdout.replaceAll('tests/test_redirect_apply_prescreen.py', 'tests/test_redirect_chain_gate.py');
      }
    });
    const result = await verify({ repository: s.root, revision: s.revision, python: '/control/python', run: control.run });
    assert.equal(result.status, 'blocked', mode);
    assert.equal(result.authorized, 12);
  }
});
