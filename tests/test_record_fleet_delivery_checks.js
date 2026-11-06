'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { execute, recordChecks } = require('../scripts/record_fleet_delivery_checks');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-command-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return { root, output: path.join(root, 'evidence') };
}

const python = path.resolve('/selected/python3.14');
const runtime = { executable: python, version: [3, 14, 10], platform: 'linux', architecture: 'x86_64' };
const zero = { exit_code: 0, signal: null, error_code: null };

test('records all four exact commands, selected runtime, logs and isolated environment', async (t) => {
  const { output } = fixture(t);
  const priorOptions = process.env.PYTEST_ADDOPTS;
  const priorPlugins = process.env.PYTEST_PLUGINS;
  process.env.PYTEST_ADDOPTS = '-k unrelated --ignore=tests/test_switch_review_fleet.py';
  process.env.PYTEST_PLUGINS = 'unrelated_plugin';
  t.after(() => {
    for (const [key, value] of [['PYTEST_ADDOPTS', priorOptions], ['PYTEST_PLUGINS', priorPlugins]]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
  let probed = false;
  const calls = [];
  const receipt = await recordChecks(output, {
    python, now: () => '2026-11-06T04:00:00.000Z',
    probe: async (selected, run, options) => {
      assert.equal(selected, python);
      assert.equal(options.env.PYTHON, python);
      probed = true;
      return runtime;
    },
    run: async (command, args, options) => {
      assert.ok(probed);
      calls.push([command, ...args]);
      assert.equal(options.env.PYTHON, python);
      assert.equal(options.env.ORCH_STANDARD_PYTHON, python);
      assert.equal(options.env.PYTEST_ADDOPTS, '-m "not slow"');
      assert.equal(options.env.PYTEST_PLUGINS, undefined);
      assert.equal(options.env.ORCH_STATE_DIR, path.join(output, 'state'));
      assert.equal(options.env.ORCH_LOCAL_RUNTIME, path.join(output, 'runtime'));
      assert.equal(options.timeout, 3600000);
      fs.writeFileSync(options.stdoutPath, 'fixture output\r\n');
      fs.writeFileSync(options.stderrPath, 'fixture diagnostic\n');
      return zero;
    },
  });
  assert.deepEqual(calls, [
    [python, '-m', 'pytest', 'tests/test_switch_review_fleet.py', '-m', 'not slow'],
    [process.execPath, 'scripts/verify_fleet_delivery_mutation.js', '--all-readers'],
    [python, 'src/verify.py'],
    [process.execPath, 'scripts/verify_fleet_delivery_live.js', path.join(output, 'live')],
  ]);
  assert.deepEqual(receipt.python_runtime, runtime);
  assert.equal(receipt.started_at, receipt.finished_at);
  assert.equal(receipt.commands_exited_zero, true);
  assert.equal(receipt.source_unchanged, true);
  assert.equal(receipt.environment.PYTHON, python);
  assert.match(receipt.source_sha256_before['scripts/record_fleet_delivery_checks.js'], /^[a-f0-9]{64}$/);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(output, 'commands.json'))), receipt);
  for (const check of receipt.checks) {
    assert.equal(fs.readFileSync(path.join(output, check.stdout), 'utf8'), 'fixture output\r\n');
    assert.equal(fs.readFileSync(path.join(output, check.stderr), 'utf8'), 'fixture diagnostic\n');
  }
  await assert.rejects(recordChecks(output, { python }), /EEXIST/);
});

test('failures, missing executables and timeouts remain failures while later checks run', async (t) => {
  const { output } = fixture(t);
  const results = [
    { ...zero, exit_code: 1 },
    { ...zero, exit_code: null, error_code: 'ENOENT' },
    { ...zero, exit_code: null, signal: 'SIGTERM' },
    zero,
  ];
  const receipt = await recordChecks(output, {
    python, timeout: 250, probe: async () => runtime,
    run: async (command, args, options) => {
      assert.equal(options.timeout, 250);
      return results.shift();
    },
  });
  assert.equal(receipt.commands_exited_zero, false);
  assert.equal(receipt.checks.length, 4);
  assert.equal(receipt.checks[1].error_code, 'ENOENT');
  assert.equal(receipt.checks[2].signal, 'SIGTERM');
  assert.equal(receipt.checks[3].exit_code, 0);
});

test('inherited ledger and Brain paths cannot override the recorded private runtime', async (t) => {
  const { root, output } = fixture(t);
  const overrides = {
    ORCH_CAPABILITIES_PATH: path.join(root, 'outside-capabilities.json'),
    ORCH_FEEDBACK_DB: path.join(root, 'outside-feedback.db'),
    ORCH_VERIFY_LIVE_STATE: '1',
  };
  const prior = Object.fromEntries(Object.keys(overrides).map((key) => [key, process.env[key]]));
  Object.assign(process.env, overrides);
  t.after(() => {
    for (const [key, value] of Object.entries(prior)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
  let calls = 0;
  const checkEnvironment = (env) => {
    for (const key of Object.keys(overrides)) assert.equal(env[key], undefined, key);
    assert.equal(env.ORCH_LOCAL_RUNTIME, path.join(output, 'runtime'));
    assert.equal(env.ORCH_STATE_DIR, path.join(output, 'state'));
  };
  const receipt = await recordChecks(output, {
    python,
    probe: async (selected, run, { env }) => {
      checkEnvironment(env);
      return runtime;
    },
    run: async (command, args, { env }) => {
      checkEnvironment(env);
      calls += 1;
      return zero;
    },
  });
  assert.equal(calls, 4);
  assert.equal(receipt.commands_exited_zero, true);
  assert.deepEqual(receipt.cleared_environment,
    [...Object.keys(overrides), 'PYTEST_PLUGINS']);
  assert.deepEqual(Object.fromEntries(Object.keys(overrides).map((key) => [key, process.env[key]])),
    overrides, 'the caller environment must remain unchanged');
});

test('interruption preserves completed checks and the unfinished attempted command', async (t) => {
  const { output } = fixture(t);
  let calls = 0;
  await assert.rejects(recordChecks(output, {
    python, probe: async () => runtime,
    run: async () => {
      if (++calls === 2) throw new Error('interrupted');
      return zero;
    },
  }), /interrupted/);
  const receipt = JSON.parse(fs.readFileSync(path.join(output, 'commands.json')));
  assert.equal(receipt.finished_at, null);
  assert.equal(receipt.commands_exited_zero, null);
  assert.equal(receipt.checks.length, 2);
  assert.equal(receipt.checks[0].exit_code, 0);
  assert.equal(receipt.checks[1].name, 'mutation');
  assert.equal(receipt.checks[1].finished_at, null);
});

test('invalid selection creates no evidence and a failed runtime probe cannot start checks', async (t) => {
  const { output } = fixture(t);
  for (const selected of ['', 'python3', 42]) {
    await assert.rejects(recordChecks(output, { python: selected }), /absolute repository-selected/);
    assert.ok(!fs.existsSync(output));
  }
  for (const timeout of [0, -1, NaN, 0.5, 2147483648]) {
    await assert.rejects(recordChecks(output, { python, timeout }), /timeout must/);
    assert.ok(!fs.existsSync(output));
  }
  await assert.rejects(recordChecks(output, {
    python, probe: async () => { throw new Error('unsupported runtime'); },
    run: async () => assert.fail('must not run checks'),
  }), /unsupported runtime/);
  const receipt = JSON.parse(fs.readFileSync(path.join(output, 'commands.json')));
  assert.equal(receipt.python_runtime, null);
  assert.deepEqual(receipt.checks, []);
  assert.equal(receipt.commands_exited_zero, null);
});

test('source changes are recorded even if every command exits zero', async (t) => {
  const { output } = fixture(t);
  let reads = 0;
  const receipt = await recordChecks(output, {
    python, probe: async () => runtime, run: async () => zero,
    readSourceHashes: () => ({ source: ++reads === 1 ? 'before' : 'after' }),
  });
  assert.equal(receipt.commands_exited_zero, true);
  assert.equal(receipt.source_unchanged, false);
  assert.deepEqual(receipt.source_sha256_after, { source: 'after' });
});

test('real subprocess output, nonzero exit, missing command and timeout are recorded', async (t) => {
  const { root } = fixture(t);
  let index = 0;
  const run = (command, args, timeout = 10000) => execute(command, args, {
    cwd: root, env: process.env, timeout,
    stdoutPath: path.join(root, `${index++}.stdout`),
    stderrPath: path.join(root, `${index}.stderr`),
  });
  assert.deepEqual(await run(process.execPath,
    ['-e', 'process.stdout.write("output"); process.stderr.write("diagnostic"); process.exit(7)']),
  { ...zero, exit_code: 7 });
  assert.equal(fs.readFileSync(path.join(root, '0.stdout'), 'utf8'), 'output');
  assert.equal(fs.readFileSync(path.join(root, '1.stderr'), 'utf8'), 'diagnostic');
  const missing = await run(path.join(root, 'absent'), []);
  assert.equal(missing.error_code, 'ENOENT');
  assert.notEqual(missing.exit_code, 0);
  const timedOut = await run(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], 100);
  assert.notEqual(timedOut.signal, null);
  assert.notEqual(timedOut.exit_code, 0);
});
