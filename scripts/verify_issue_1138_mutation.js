'use strict';

// Repeat the corrected corrupt-artifact test's deliberate break. Infrastructure
// failures must never replace the historical evidence with a claimed red/green run.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { withMutation } = require('./verify_redirect_routing_mutations');

const testFile = 'tests/test_coverage_round_1138.py';
const test = 'test_build_report_treats_corrupt_pattern_artifacts_as_empty';
const node = `${testFile}::${test}`;
const edit = {
  file: 'src/periodic_report.py',
  before: '        pattern_status = {}',
  after: '        pattern_status = {"broken": True}',
};
const args = ['-B', '-m', 'pytest', '-v', '--tb=short', '--color=no', node,
  '-m', 'not slow', '-p', 'no:cacheprovider'];
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

function runPytest(root) {
  return spawnSync(process.env.PYTHON || 'python3', args, {
    cwd: root,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
    encoding: 'utf8', timeout: 60000, maxBuffer: 4 * 1024 * 1024,
  });
}

function requireVerdict(result, failed) {
  const output = (result.stdout || '') + (result.stderr || '');
  assert.ok(!result.error, `${result.error?.message}\n${output}`);
  assert.equal(result.status, failed ? 1 : 0, output);
  const rows = output.split('\n').filter((line) => line.startsWith(`${testFile}::`));
  assert.equal(rows.length, 1, output);
  assert.ok(rows[0].startsWith(`${node} ${failed ? 'FAILED' : 'PASSED'} `), output);
  assert.match(output, failed ? /\b1 failed\b/ : /\b1 passed\b/);
  assert.doesNotMatch(output, /\b\d+ (?:errors?|skipped|xfailed|xpassed|deselected)\b/i);
  if (failed) {
    assert.match(output, /AssertionError: assert \{['"]broken['"]: True\} == \{\}/);
  }
  return output;
}

function verify(root, run = runPytest) {
  const source = path.join(root, edit.file);
  const original = fs.readFileSync(source);
  const testSource = fs.readFileSync(path.join(root, testFile));
  const baseline = requireVerdict(run(root), false);
  const red = withMutation(root, { edits: [edit] }, () => requireVerdict(run(root), true));
  assert.deepEqual(fs.readFileSync(source), original, 'production source was not restored');
  const green = requireVerdict(run(root), false);
  assert.deepEqual(fs.readFileSync(source), original, 'restored test changed production source');
  assert.deepEqual(fs.readFileSync(path.join(root, testFile)), testSource, 'test source changed');
  return {
    test, file: edit.file, before: edit.before, broken: edit.after,
    command: [process.env.PYTHON || 'python3', ...args],
    source_sha256: digest(original), restored_source_sha256: digest(fs.readFileSync(source)),
    test_sha256: digest(testSource),
    baseline_exit: 0, baseline_output: baseline,
    red_exit: 1, red_output: red, green_exit: 0, green_output: green,
  };
}

function main() {
  const root = path.resolve(__dirname, '..');
  const head = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' });
  assert.equal(head.status, 0, head.stderr);
  const clean = spawnSync('git', ['diff', 'HEAD', '--exit-code', '--', edit.file, testFile], {
    cwd: root, encoding: 'utf8',
  });
  assert.equal(clean.status, 0, 'commit source/test changes before recording exact-head evidence');
  const evidence = path.join(root, 'docs/verification/issue-1138/mutation-proof-pass3.json');
  const record = JSON.parse(fs.readFileSync(evidence, 'utf8'));
  assert.ok(record.followup_runs === undefined || Array.isArray(record.followup_runs));
  const result = verify(root);
  record.followup_runs = [...(record.followup_runs || []), {
    measured_head_sha: head.stdout.trim(), recorded_at: new Date().toISOString(), cases: [result],
  }];
  fs.writeFileSync(evidence, `${JSON.stringify(record, null, 2)}\n`);
  console.log(`${node}: baseline passed; mutation failed; restored passed; evidence appended`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`Issue 1138 mutation verification blocked or failed: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { edit, node, requireVerdict, verify };
