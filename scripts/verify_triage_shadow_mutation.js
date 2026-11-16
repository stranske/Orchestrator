'use strict';

// Capture issue #1399's actual rule-pass removal with the selected Python.
// Missing pytest, collection failures and unrelated assertions are not evidence.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { withMutation } = require('./verify_redirect_routing_mutations');
const { requireVerdict: requireSingleTestVerdict } = require('./verify_issue_1138_mutation');

const testFile = 'tests/test_triage_shadow.py';
const node = `${testFile}::test_linker_resolves_rule_picks_within_the_read_bound`;
const edit = {
  file: 'src/outcomes.py',
  before: '        _resolve_rule_pick_covers(conn, corpus_rows, search)\n',
  after: '        pass  # deliberate rule-pass removal\n',
};
const args = ['-B', '-m', 'pytest', node, '-v', '--tb=short', '--color=no',
  '-m', 'not slow', '-p', 'no:cacheprovider'];
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

function runPytest(root, python, cache) {
  return spawnSync(python, args, {
    cwd: root,
    // A fresh cache prefix prevents reading stale .pyc files after source removal.
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONPYCACHEPREFIX: cache },
    encoding: 'utf8', timeout: 60000, maxBuffer: 4 * 1024 * 1024,
  });
}

function requireVerdict(result, failed) {
  assert.ok(!result.signal, `pytest terminated by ${result.signal}`);
  return requireSingleTestVerdict(result, failed, {
    testNode: node,
    failurePattern: /E\s+KeyError: ['"]pr_reads['"]/,
  });
}

function verify(root, python, run = runPytest) {
  assert.ok(typeof python === 'string' && path.isAbsolute(python),
    'set PYTHON to the absolute repository-selected interpreter');
  const source = path.join(root, edit.file);
  const original = fs.readFileSync(source);
  const testSource = fs.readFileSync(path.join(root, testFile));
  const newline = original.includes(Buffer.from('\r\n')) ? '\r\n' : '\n';
  const mutation = Object.fromEntries(Object.entries(edit)
    .map(([key, value]) => [key, value.replaceAll('\n', newline)]));
  assert.equal(original.toString('utf8').split(mutation.before).length - 1, 1,
    'the actual rule-pass invocation must occur exactly once');
  const cache = fs.mkdtempSync(path.join(os.tmpdir(), 'triage-mutation-bytecode-'));
  function requireInputs(expected) {
    assert.deepEqual(fs.readFileSync(source), expected, 'production source changed during pytest');
    assert.deepEqual(fs.readFileSync(path.join(root, testFile)), testSource,
      'test source changed during pytest');
  }
  try {
    const baseline = requireVerdict(run(root, python, cache), false);
    requireInputs(original);
    let broken;
    const red = withMutation(root, { edits: [mutation] }, () => {
      broken = fs.readFileSync(source);
      const output = requireVerdict(run(root, python, cache), true);
      requireInputs(broken);
      return output;
    });
    requireInputs(original);
    const green = requireVerdict(run(root, python, cache), false);
    requireInputs(original);
    return {
      command: [python, ...args], file: edit.file, before: mutation.before, broken: mutation.after,
      source_sha256: digest(original), broken_source_sha256: digest(broken),
      restored_source_sha256: digest(fs.readFileSync(source)), test_sha256: digest(testSource),
      baseline_exit: 0, baseline_output: baseline,
      red_exit: 1, red_output: red, green_exit: 0, green_output: green,
    };
  } finally {
    if (!fs.existsSync(source) || !fs.readFileSync(source).equals(original)) {
      fs.writeFileSync(source, original);
    }
    fs.rmSync(cache, { recursive: true, force: true });
  }
}

if (require.main === module) {
  try {
    const root = path.resolve(__dirname, '..');
    const head = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' });
    assert.equal(head.status, 0, head.stderr);
    const clean = spawnSync('git', ['diff', 'HEAD', '--exit-code', '--', edit.file, testFile], {
      cwd: root, encoding: 'utf8',
    });
    assert.equal(clean.status, 0, 'commit source/test changes before recording head-bound evidence');
    const result = verify(root, process.env.PYTHON);
    // Emit a receipt only after all three pytest runs have the intended verdicts.
    process.stdout.write(`${JSON.stringify({
      measured_head_sha: head.stdout.trim(), recorded_at: new Date().toISOString(), ...result,
    }, null, 2)}\n`);
  } catch (error) {
    console.error(`Triage mutation verification blocked or failed: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { edit, node, requireVerdict, verify };
