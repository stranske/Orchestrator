'use strict';

// Audit recovered #771 measurements offline. This does not run the historical
// tests or turn a single post-change report into a controlled comparison.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const head = '18ad97a054dab7f6039dd42c8131bee04267748b';
const baseline = '3b379f8b60046b1b6b21b360a931472d0b6db9c3';
const configurations = {
  'pyproject.toml': 'cdf71323ecb03f631f9bf2f8734146a10d95c3e0',
  '.github/workflows/ci.yml': 'c95ad637961ab568f680da5908771376f52b11e5',
  'src/verify.py': 'a90705e480de5365ed5963f502a3cfb4f31b5571',
};
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const gitBlob = bytes => crypto.createHash('sha1')
  .update(`blob ${bytes.length}\0`).update(bytes).digest('hex');

function verifyCoverage(directory) {
  const records = JSON.parse(fs.readFileSync(path.join(directory, 'records.json'), 'utf8'));
  assert.equal(records.head_commit, head, 'wrong original head');
  assert.equal(records.baseline_commit, baseline, 'wrong requested baseline');
  const read = file => {
    const bytes = fs.readFileSync(path.join(directory, file));
    assert.equal(digest(bytes), records.files[file], `receipt digest mismatch: ${file}`);
    return bytes;
  };
  assert.equal(records.configuration_snapshots.length, 3, 'incomplete configuration');
  assert.deepEqual(records.configuration_snapshots.map(s => s.source).sort(),
    Object.keys(configurations).sort(), 'wrong configuration population');
  for (const snapshot of records.configuration_snapshots) {
    const actual = gitBlob(read(snapshot.retained_as));
    assert.equal(actual, configurations[snapshot.source], 'historical configuration blob mismatch');
    assert.equal(snapshot.head_git_blob_sha, actual, 'head configuration mismatch');
    assert.equal(snapshot.baseline_git_blob_sha, actual, 'baseline configuration mismatch');
    assert.equal(snapshot.identical_bytes, true, 'configuration differs');
    assert.equal(snapshot.baseline_exit_code, 0, 'baseline inspection failed');
    assert.deepEqual(snapshot.baseline_command,
      ['git', 'show', `${baseline}:${snapshot.source}`], 'wrong baseline inspection command');
  }
  const config = records.configuration_snapshots.find(s => s.source === 'pyproject.toml');
  assert.match(read(config.retained_as).toString('utf8'), /^branch = false$/m,
    'statement-only historical configuration required');

  // The CI manifest authenticates the retained metadata bytes independently of
  // the coverage annotations. It is a consistency check, not a signature.
  const ciDirectory = path.resolve(directory, '../ci');
  const ci = JSON.parse(fs.readFileSync(path.join(ciDirectory, 'manifest.json'), 'utf8'));
  const metadata = file => {
    const bytes = fs.readFileSync(path.join(ciDirectory, file));
    assert.equal(digest(bytes), ci.files[file], `CI metadata digest mismatch: ${file}`);
    return JSON.parse(bytes.toString('utf8'));
  };
  const job = records.coverage_job;
  assert.equal(job.run_id, 39180639406, 'wrong coverage run');
  assert.equal(job.job_id, 117832439523, 'wrong coverage job');
  const run = metadata(`run-${job.run_id}.json`);
  assert.equal(run.head_sha, head, 'coverage run head mismatch');
  assert.equal(run.name, 'CI', 'wrong coverage workflow');
  const actualJob = metadata(`jobs-${job.run_id}.json`).jobs.find(j => j.id === job.job_id);
  assert.ok(actualJob, 'missing coverage job');
  assert.equal(actualJob.run_id, job.run_id, 'coverage job belongs to another run');
  assert.equal(actualJob.status, 'completed', 'coverage job incomplete');
  assert.equal(actualJob.conclusion, 'success', 'coverage job unsuccessful');
  assert.equal(actualJob.name, job.name, 'coverage job name mismatch');
  assert.equal(actualJob.steps.find(s => s.name === 'Measure combined coverage')?.conclusion,
    'success', 'coverage measurement step unsuccessful');
  assert.equal(job.job_conclusion, actualJob.conclusion, 'incorrect job result annotation');
  assert.equal(job.process_exit_code, null, 'numeric process exit was not preserved');

  const lines = read(job.log).toString('utf8').replace(/\x1b\[[0-9;]*m/g, '').split('\n')
    .map(line => line.replace(/^\uFEFF?\d{4}-\d{2}-\d{2}T\S+Z /, ''));
  assert.ok(lines.includes(`Complete job name: ${job.name}`), 'coverage log job mismatch');
  assert.equal(job.command,
    'python3 src/verify.py --coverage --floor-may-lag 2>&1 | tee /tmp/combined.txt',
    'wrong measurement command');
  assert.ok(lines.includes(job.command), 'measurement command missing from log');
  const checkoutIndex = lines.findIndex(line => /^\[command\].*\/git log -1 --format=%H$/.test(line));
  assert.ok(checkoutIndex >= 0, 'missing measured checkout');
  const checkout = lines[checkoutIndex + 1];
  assert.match(checkout, /^[a-f0-9]{40}$/, 'invalid measured checkout');
  assert.equal(records.observed_checkout_sha, checkout, 'checkout annotation mismatch');
  assert.ok(lines.includes('Name                                   Stmts   Miss  Cover'),
    'statement report header missing');
  const one = pattern => {
    const matches = lines.map(line => line.match(pattern)).filter(Boolean);
    assert.equal(matches.length, 1, `missing or ambiguous measurement: ${pattern}`);
    return matches[0].slice(1).map(Number);
  };
  const [statements, missing, percent] = one(/^src\/feedback\.py\s+(\d+)\s+(\d+)\s+(\d+\.\d+)%$/);
  assert.ok(statements > 0 && missing <= statements, 'invalid statement counts');
  const covered = statements - missing;
  assert.equal(Number((covered / statements * 100).toFixed(1)), percent,
    'statement percentage disagrees with counts');
  const [passed, failed, skipped, collected] = one(
    /^\s*pytest:\s+(\d+) passed, (\d+) failed, (\d+)\/\d+ max skipped \((\d+) collected;/);
  assert.equal(failed, 0, 'pytest failed');
  assert.equal(passed + skipped, collected, 'pytest population totals disagree');
  const [ran, total, skippedModules] = one(/^\s*selftests:\s+(\d+) of (\d+) modules ran, (\d+)\/\d+ max skipped$/);
  assert.equal(ran + skippedModules, total, 'selftest population totals disagree');
  const [processes] = one(/^coverage: combined (\d+) instrumented process\(es\)$/);
  const measured = {
    num_statements: statements, missing_statements: missing, covered_statements: covered,
    reported_statement_percent: percent,
    pytest_collected: collected, pytest_passed: passed, pytest_skipped: skipped,
    selftests_ran: ran, selftests_total: total, selftests_skipped: skippedModules,
    instrumented_processes: processes,
  };
  for (const [key, value] of Object.entries(measured)) {
    assert.equal(records.observations[key], value, `observation disagrees with raw log: ${key}`);
  }
  const unknown = {
    num_branches: null, missing_branches: null, comparable_baseline: null,
    missing_statement_delta: null, combined_unit_delta: null,
    shortfall_against_180_combined_units: null,
  };
  for (const key of Object.keys(unknown)) {
    assert.equal(records.observations[key], null, `unsupported measurement or comparison: ${key}`);
  }
  return {
    kind: 'audit of recovered history; not a new coverage execution',
    workflow: run.name, run_id: run.id, job_id: actualJob.id,
    run_head: head, checkout_sha: checkout, exact_head_execution: checkout === head,
    configuration_identical: true, comparable: false,
    exact_test_population: null, dependency_versions: null,
    observations: { ...measured, ...unknown },
    limitations: [
      'Only whole-job population totals are preserved; exact test identities are unknown.',
      'No matching baseline report is preserved; identical configuration is insufficient.',
      'The five tests and production fix are not isolated from background changes.',
      'Branch coverage was disabled; combined-unit deltas and target shortfall are unknown.',
    ],
  };
}

function main(args, stdout, stderr) {
  try {
    assert.ok(args.length <= 1, 'usage: node scripts/verify_issue_771_coverage.js [DIRECTORY]');
    const directory = args[0] || path.resolve(__dirname,
      '../docs/evidence/issue-771-repair/coverage');
    stdout(`${JSON.stringify(verifyCoverage(directory), null, 2)}\n`);
    return 0;
  } catch (error) {
    stderr(`${error.message}\n`);
    return 1;
  }
}

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2),
    text => process.stdout.write(text), text => process.stderr.write(text));
}

module.exports = { verifyCoverage, main };
