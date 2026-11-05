'use strict';

// Reproduce issue #672's fail-open control without changing the working tree.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');

const testClass = 'FleetDeliveryTests';
const testModule = 'test_switch_review_fleet';
const controls = {
  gate_cancellations: {
    signature: '_fleet_gate_cancellations(gh, now: int)', args: 'gh, now',
    testName: 'test_failed_gate_read_is_not_a_number',
  },
  merge_to_close: {
    signature: '_fleet_pr_measures(gh, now: int, repos: list[str], *, flow: bool)',
    args: 'gh, now, repos, flow=flow', flow: false,
    testName: 'test_each_gh_failure_preserves_every_row',
  },
  oldest_canary: {
    signature: '_fleet_oldest_canary(gh, now: int)', args: 'gh, now',
    testName: 'test_each_gh_failure_preserves_every_row',
  },
  opener_cap: {
    signature: '_fleet_opener_cap(runner)', args: 'runner',
    testName: 'test_opener_failure_does_not_stop_other_rows',
  },
  released_flow: {
    signature: '_fleet_pr_measures(gh, now: int, repos: list[str], *, flow: bool)',
    args: 'gh, now, repos, flow=flow', flow: true,
    testName: 'test_each_gh_failure_preserves_every_row',
  },
};

function mutation(control) {
  const name = control.signature.split('(')[0];
  const anchor = `def ${control.signature} -> dict:\n`;
  // The PR readers share a function. Mutate only the selected arm, so each verdict
  // demonstrates that arm's failure is detected independently of the other one.
  const guard = control.flow === undefined ? ''
    : `        if flow is ${control.flow ? 'False' : 'True'}:\n            raise\n`;
  const mutant = `${anchor}    try:
        return ${name}_original(${control.args})
    except _FleetReadError:
${guard}        return _fleet_measured("0%", "deliberate fail-open control")


def ${control.signature.replace(name, `${name}_original`)} -> dict:
`;
  return { anchor, mutant };
}

const { anchor } = mutation(controls.gate_cancellations);

function requireVerdict(result, runner, failed, control = controls.gate_cancellations) {
  const { testName } = control;
  const output = (result.stdout || '') + (result.stderr || '');
  assert.ok(!result.error, `${result.error?.message}\n${output}`);
  assert.equal(result.signal, null, output);
  assert.equal(result.status, failed ? 1 : 0, output);
  if (runner === 'pytest') {
    const node = `tests/${testModule}.py::${testClass}::${testName}`;
    assert.ok(output.includes(`${node} ${failed ? 'FAILED' : 'PASSED'}`), output);
    const counts = [...output.matchAll(/\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b/gi)];
    assert.deepEqual(counts.map((match) => [Number(match[1]), match[2].toLowerCase()]),
      [[1, failed ? 'failed' : 'passed']], output);
  } else {
    const name = `${testName} (${testModule}.${testClass}.${testName})`;
    assert.ok(output.includes(`${name} ... ${failed ? '' : 'ok'}`), output);
    assert.match(output, /Ran 1 test\b/);
    if (failed) {
      // unittest reports each failing subtest separately, while still running one
      // selected method. All failures must belong to that method.
      const summary = output.match(/FAILED \(failures=([1-9]\d*)\)/);
      assert.ok(summary, output);
      const failures = [...output.matchAll(/^FAIL: (.+)$/gm)].map((match) => match[1]);
      assert.equal(failures.length, Number(summary[1]), output);
      assert.ok(failures.every((failure) => failure === name || failure.startsWith(`${name} (`)), output);
    } else {
      assert.match(output, /\nOK\s*$/);
    }
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

function runTest(root, runner, python, control) {
  const { testName } = control;
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

async function verify(root, runner = 'pytest', python = process.env.PYTHON || '/opt/anaconda3/bin/python3', run = runTest, control = controls.gate_cancellations) {
  assert.ok(['pytest', 'unittest'].includes(runner), 'unknown test runner');
  const { anchor, mutant } = mutation(control);
  const file = path.join(root, 'src/switch_review.py');
  const original = fs.readFileSync(file);
  const source = original.toString('utf8');
  assert.equal(source.split(anchor).length - 1, 1, 'reader mutation anchor must occur once');
  requireVerdict(await run(root, runner, python, control), runner, false, control);
  try {
    fs.writeFileSync(file, source.replace(anchor, mutant));
    requireVerdict(await run(root, runner, python, control), runner, true, control);
  } finally {
    fs.writeFileSync(file, original);
    assert.deepEqual(fs.readFileSync(file), original, 'source restoration changed bytes');
  }
  requireVerdict(await run(root, runner, python, control), runner, false, control);
  return `${runner}: baseline passed; fail-open mutation detected; restored test passed`;
}

async function runIsolated(argv = process.argv.slice(2), python = process.env.PYTHON || '/opt/anaconda3/bin/python3') {
  assert.ok(new Set(argv).size === argv.length
    && argv.every((arg) => ['--unittest', '--all-readers'].includes(arg)),
  'usage: node scripts/verify_fleet_delivery_mutation.js [--unittest] [--all-readers]');
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
    const runner = argv.includes('--unittest') ? 'unittest' : 'pytest';
    if (!argv.includes('--all-readers')) {
      return await verify(sandbox, runner, python);
    }
    const verdicts = [];
    for (const [key, control] of Object.entries(controls)) {
      verdicts.push(`${key}: ${await verify(sandbox, runner, python, runTest, control)}`);
    }
    return verdicts.join('\n');
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

module.exports = { anchor, controls, mutation, requireVerdict, runIsolated, verify };
