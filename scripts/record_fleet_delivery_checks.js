'use strict';

// Collect issue #672's required commands on the machine being verified. These
// receipts describe this host; a runner receipt does not establish target access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { createHash } = require('node:crypto');
const { parseArgs } = require('node:util');
const { fleetSourceHashes, pythonRuntime } = require('./verify_fleet_delivery_live');

function execute(command, args, { cwd, env, timeout, stdoutPath, stderrPath }) {
  // Stream into files: full verification can exceed execFile's output buffer.
  const stdout = fs.openSync(stdoutPath, 'wx');
  let stderr;
  return new Promise((resolve, reject) => {
    let child;
    try {
      stderr = fs.openSync(stderrPath, 'wx');
      child = spawn(command, args, { cwd, env, timeout, stdio: ['ignore', stdout, stderr] });
    } catch (error) {
      fs.closeSync(stdout);
      if (stderr !== undefined) fs.closeSync(stderr);
      reject(error);
      return;
    }
    let errorCode = null;
    child.on('error', (error) => { errorCode = error.code || 'UNKNOWN'; });
    child.on('close', (code, signal) => {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
      // spawn's timeout sends a signal, but a child may handle it and exit zero.
      // This function never sends any other signal, so killed identifies timeout
      // delivery even when close reports a normal exit without a signal.
      resolve({ exit_code: code, signal, error_code: errorCode, timed_out: child.killed });
    });
  });
}

function sourceHashes(root) {
  return {
    ...fleetSourceHashes(root),
    ...Object.fromEntries([
      'src/verify.py', 'pyproject.toml', 'scripts/verify_fleet_delivery_mutation.js',
      'scripts/record_fleet_delivery_checks.js',
    ].map((file) => [file,
      createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex'),
    ])),
  };
}

async function recordChecks(outputDir, {
  python = process.env.PYTHON,
  timeout = 3600000,
  run = execute,
  probe = pythonRuntime,
  now = () => new Date().toISOString(),
  readSourceHashes = sourceHashes,
} = {}) {
  assert.ok(typeof python === 'string' && path.isAbsolute(python),
    'set PYTHON to the absolute repository-selected interpreter');
  assert.ok(Number.isSafeInteger(timeout) && timeout > 0 && timeout <= 2147483647,
    'timeout must be a positive integer of at most 2147483647 milliseconds');
  const root = path.resolve(__dirname, '..');
  const destination = path.resolve(outputDir);
  fs.mkdirSync(destination); // A failed attempt must never reuse old evidence.
  const env = {
    ...process.env,
    PYTHON: python,
    ORCH_STANDARD_PYTHON: python,
    PYTEST_ADDOPTS: '-m "not slow"',
    ORCH_LOCAL_RUNTIME: path.join(destination, 'runtime'),
    ORCH_STATE_DIR: path.join(destination, 'state'),
  };
  // Explicit ledger/Brain paths win over ORCH_LOCAL_RUNTIME. Drop inherited
  // paths so every check uses the recorded private runtime; keep verify.py's
  // default private-state snapshot enabled. Also discard inherited test plugins.
  const clearedEnvironment = [
    'ORCH_CAPABILITIES_PATH', 'ORCH_FEEDBACK_DB', 'ORCH_VERIFY_LIVE_STATE', 'PYTEST_PLUGINS',
  ];
  for (const key of clearedEnvironment) delete env[key];
  const receipt = {
    started_at: now(), finished_at: null,
    selected_interpreter: python, python_runtime: null,
    environment: Object.fromEntries(['PYTHON', 'ORCH_STANDARD_PYTHON', 'PYTEST_ADDOPTS',
      'ORCH_LOCAL_RUNTIME', 'ORCH_STATE_DIR'].map((key) => [key, env[key]])),
    cleared_environment: clearedEnvironment,
    source_sha256_before: readSourceHashes(root), source_sha256_after: null,
    source_unchanged: null, commands_exited_zero: null, checks: [],
  };
  const save = () => fs.writeFileSync(path.join(destination, 'commands.json'),
    JSON.stringify(receipt, null, 2) + '\n');
  save();
  // A failed runtime probe leaves an explicitly unfinished receipt, never a
  // fallback interpreter. Reuse the live verifier's standard-version policy.
  receipt.python_runtime = await probe(python, undefined, { cwd: root, env });
  save();
  const commands = [
    ['pytest', python, ['-m', 'pytest', 'tests/test_switch_review_fleet.py', '-m', 'not slow']],
    ['mutation', process.execPath, ['scripts/verify_fleet_delivery_mutation.js', '--all-readers']],
    ['verify', python, ['src/verify.py']],
    ['live', process.execPath, ['scripts/verify_fleet_delivery_live.js', path.join(destination, 'live')]],
  ];
  for (const [name, command, args] of commands) {
    const check = {
      name, command: [command, ...args], started_at: now(), finished_at: null,
      timeout_ms: timeout, exit_code: null, signal: null, error_code: null, timed_out: null,
      stdout: `${name}.stdout.log`, stderr: `${name}.stderr.log`,
    };
    receipt.checks.push(check);
    save(); // Preserve the attempted command even if this process is interrupted.
    Object.assign(check, await run(command, args, {
      cwd: root, env, timeout,
      stdoutPath: path.join(destination, check.stdout),
      stderrPath: path.join(destination, check.stderr),
    }), { finished_at: now() });
    save(); // Retain failed checks and continue collecting independent evidence.
  }
  receipt.source_sha256_after = readSourceHashes(root);
  receipt.source_unchanged = JSON.stringify(receipt.source_sha256_before)
    === JSON.stringify(receipt.source_sha256_after);
  receipt.commands_exited_zero = receipt.checks.every((check) => check.exit_code === 0
    && check.signal === null && check.error_code === null && check.timed_out === false);
  receipt.finished_at = now();
  save();
  return receipt;
}

if (require.main === module) {
  (async () => {
    const { values, positionals } = parseArgs({
      options: { 'timeout-ms': { type: 'string' } }, allowPositionals: true,
    });
    assert.equal(positionals.length, 1,
      'usage: node scripts/record_fleet_delivery_checks.js NEW_OUTPUT_DIRECTORY [--timeout-ms N]');
    const receipt = await recordChecks(positionals[0], {
      timeout: values['timeout-ms'] === undefined ? 3600000 : Number(values['timeout-ms']),
    });
    console.log(`Fleet command receipts: ${path.resolve(positionals[0], 'commands.json')}`);
    // Zero exits are recorded literally. The logs and live receipt still need
    // review for test verdicts and confirmation that this was the target host.
    if (!receipt.commands_exited_zero || !receipt.source_unchanged) process.exitCode = 1;
  })().catch((error) => {
    console.error(`Fleet command recording blocked or failed: ${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = { execute, recordChecks };
