'use strict';

// Verify the two frozen coverage-record locks independently. Only a successful
// baseline, the intended assertion failure, and a restored pass count as evidence.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { withMutation } = require('./verify_redirect_routing_mutations');
const { requireVerdict, withArchivedInputs } = require('./verify_issue_1138_mutation');

const testFile = 'tests/test_coverage_round_1138.py';
const directory = 'docs/verification/issue-1138';
const experiments = [
  {
    test: 'test_published_head_measurement_matches_ranked_source_lines',
    file: `${directory}/head-ea6255b-combined.json`,
    field: 'gain',
    failurePattern: /(?:AssertionError: )?assert 296 == 297\b/,
  },
  {
    test: 'test_current_head_remeasurement_keeps_ranked_gaps_inside_selftests',
    file: `${directory}/head-ecb0707-combined.json`,
    field: 'gain_vs_base',
    failurePattern: /(?:AssertionError: )?assert 297 == 296\b/,
  },
];
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const args = (test) => ['-B', '-m', 'pytest', '-v', '--tb=short', '--color=no',
  `${testFile}::${test}`, '-m', 'not slow', '-p', 'no:cacheprovider'];

function runPytest(root, test) {
  return spawnSync(process.env.PYTHON || 'python3', args(test), {
    cwd: root,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
    encoding: 'utf8', timeout: 60000, maxBuffer: 4 * 1024 * 1024,
  });
}

function verify(root, run = runPytest) {
  const originals = new Map(experiments.map(({ file }) => [
    file, fs.readFileSync(path.join(root, file)),
  ]));
  const testSource = fs.readFileSync(path.join(root, testFile));
  function requireInputs(activeFile, activeBytes) {
    for (const [file, original] of originals) {
      const expected = file === activeFile ? activeBytes : original;
      assert.deepEqual(fs.readFileSync(path.join(root, file)), expected,
        `coverage record changed: ${file}`);
    }
    assert.deepEqual(fs.readFileSync(path.join(root, testFile)), testSource,
      'test source changed');
  }
  try {
    return experiments.map(({ test, file, field, failurePattern }) => {
      requireInputs();
      const original = originals.get(file);
      const record = JSON.parse(original.toString('utf8'));
      assert.equal(record[field], 296, `unexpected starting value: ${file}:${field}`);
      const options = { testNode: `${testFile}::${test}`, failurePattern };
      const baseline = requireVerdict(run(root, test), false, options);
      requireInputs();
      const broken = `${JSON.stringify({ ...record, [field]: 297 }, null, 2)}\n`;
      const red = withMutation(root, { edits: [{
        file, before: original.toString('utf8'), after: broken,
      }] }, () => {
        const output = requireVerdict(run(root, test), true, options);
        requireInputs(file, Buffer.from(broken));
        return output;
      });
      requireInputs();
      const green = requireVerdict(run(root, test), false, options);
      requireInputs();
      return {
        test, file, field, before: 296, broken: 297,
        command: [process.env.PYTHON || 'python3', ...args(test)],
        record_sha256: digest(original), broken_record_sha256: digest(broken),
        restored_record_sha256: digest(fs.readFileSync(path.join(root, file))),
        test_sha256: digest(testSource),
        baseline_exit: 0, baseline_output: baseline,
        red_exit: 1, red_output: red, green_exit: 0, green_output: green,
      };
    });
  } finally {
    // The runner can change either record, even outside the deliberate mutation.
    for (const [file, original] of originals) {
      const recordPath = path.join(root, file);
      if (!fs.existsSync(recordPath) || !fs.readFileSync(recordPath).equals(original)) {
        fs.writeFileSync(recordPath, original);
      }
    }
  }
}

function verifyAtHead(root, head, run = runPytest) {
  const files = experiments.map(({ file }) => file);
  return withArchivedInputs(root, head, files, (checkout) => verify(checkout, run));
}

function main() {
  const root = path.resolve(__dirname, '..');
  const head = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' });
  assert.equal(head.status, 0, head.stderr);
  const measuredHead = head.stdout.trim();
  const clean = spawnSync('git', ['diff', measuredHead, '--exit-code', '--', 'src',
    testFile, ...experiments.map(({ file }) => file)], { cwd: root, encoding: 'utf8' });
  assert.equal(clean.status, 0, 'commit source/test/record changes before recording exact-head evidence');
  const cases = verifyAtHead(root, measuredHead);
  // Print only after both experiments finish, so partial runs cannot masquerade
  // as a complete acceptance record. Redirect stdout to a disposable evidence file.
  process.stdout.write(`${JSON.stringify({
    measured_head_sha: measuredHead, recorded_at: new Date().toISOString(), cases,
  }, null, 2)}\n`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`Issue 1138 artifact verification blocked or failed: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { experiments, verify, verifyAtHead };
