'use strict';

// Reproduce issue #672's fail-open control without changing the working tree.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');

const testName = 'test_failed_gate_read_is_not_a_number';
const testClass = 'FleetDeliveryTests';
const testModule = 'test_switch_review_fleet';
const anchor = 'def _fleet_gate_cancellations(gh, now: int) -> dict:\n';
const mutant = `${anchor}    try:
        return _fleet_gate_cancellations_original(gh, now)
    except _FleetReadError:
        return _fleet_measured("0%", "deliberate fail-open control")


def _fleet_gate_cancellations_original(gh, now: int) -> dict:
`;

function requireVerdict(result, runner, failed) {
  const output = (result.stdout || '') + (result.stderr || '');
  assert.ok(!result.error, `${result.error?.message}\n${output}`);
  assert.equal(result.signal, null, output);
  assert.equal(result.status, failed ? 1 : 0, output);
  if (runner === 'pytest') {
    const node = `tests/${testModule}.py::${testClass}::${testName}`;
    assert.ok(output.includes(`${node} ${failed ? 'FAILED' : 'PASSED'}`), output);
    assert.match(output, failed ? /\b1 failed\b/ : /\b1 passed\b/);
    assert.doesNotMatch(output, /\b\d+ (?:error|skipped|xfailed|xpassed|deselected)/i);
  } else {
    const name = `${testName} (${testModule}.${testClass}.${testName})`;
    assert.ok(output.includes(`${name} ... ${failed ? 'FAIL' : 'ok'}`), output);
    assert.match(output, /Ran 1 test\b/);
    assert.match(output, failed ? /FAILED \(failures=1\)/ : /\nOK\s*$/);
    assert.doesNotMatch(output, /\b(?:ERROR|skipped)\b/);
  }
  if (failed) {
    // An import failure or a different assertion is not proof of detecting the mutant.
    assert.match(output, /AssertionError: '0%' is not None/);
  }
}

function runCommand(command, args, options) {
  return new Promise((resolve) => {
    execFile(command, args, options, (error, stdout, stderr) => resolve({
      status: error ? (Number.isInteger(error.code) ? error.code : null) : 0,
      signal: error?.signal || null,
      error: error && !Number.isInteger(error.code) ? error : undefined,
      stdout, stderr,
    }));
  });
}

function runTest(root, runner, python) {
  const args = runner === 'pytest'
    ? ['-B', '-m', 'pytest', `tests/${testModule}.py::${testClass}::${testName}`,
      '-v', '-m', 'not slow', '-p', 'no:cacheprovider']
    : ['-B', '-m', 'unittest', '-v', `${testModule}.${testClass}.${testName}`];
  return runCommand(python, args, {
    cwd: root,
    env: {
      ...process.env,
      PYTHONPATH: [path.join(root, 'src'), path.join(root, 'tests')].join(path.delimiter),
      PYTHONDONTWRITEBYTECODE: '1',
      ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
      ORCH_STATE_DIR: path.join(root, 'state'),
    },
    encoding: 'utf8', timeout: 60000, maxBuffer: 4 * 1024 * 1024,
  });
}

async function verify(root, runner = 'pytest', python = process.env.PYTHON || '/opt/anaconda3/bin/python3', run = runTest) {
  assert.ok(['pytest', 'unittest'].includes(runner), 'unknown test runner');
  const file = path.join(root, 'src/switch_review.py');
  const original = fs.readFileSync(file);
  const source = original.toString('utf8');
  assert.equal(source.split(anchor).length - 1, 1, 'Gate mutation anchor must occur once');
  requireVerdict(await run(root, runner, python), runner, false);
  try {
    fs.writeFileSync(file, source.replace(anchor, mutant));
    requireVerdict(await run(root, runner, python), runner, true);
  } finally {
    fs.writeFileSync(file, original);
    assert.deepEqual(fs.readFileSync(file), original, 'source restoration changed bytes');
  }
  requireVerdict(await run(root, runner, python), runner, false);
  return `${runner}: baseline passed; fail-open mutation detected; restored test passed`;
}

async function runIsolated(argv = process.argv.slice(2), python = process.env.PYTHON || '/opt/anaconda3/bin/python3') {
  assert.ok(argv.length === 0 || (argv.length === 1 && argv[0] === '--unittest'),
    'usage: node scripts/verify_fleet_delivery_mutation.js [--unittest]');
  const repo = path.resolve(__dirname, '..');
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-delivery-mutation-'));
  try {
    for (const entry of ['src', 'config', 'tests/fixtures', `tests/${testModule}.py`,
      'pyproject.toml']) {
      fs.cpSync(path.join(repo, entry), path.join(sandbox, entry), {
        recursive: true,
        filter: (file) => !/(secret|token|credential|__pycache__)/i.test(path.basename(file)),
      });
    }
    return await verify(sandbox, argv.length ? 'unittest' : 'pytest', python);
  } finally {
    fs.rmSync(sandbox, { recursive: true, force: true });
  }
}

if (require.main === module) {
  runIsolated().then((result) => console.log(result)).catch((error) => {
    console.error(`Fleet mutation verification blocked or failed: ${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = { anchor, requireVerdict, runIsolated, verify };
