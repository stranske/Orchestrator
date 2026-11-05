'use strict';

// Repository-specific, offline verification of the recovered #771 CI records.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const originalHead = '18ad97a054dab7f6039dd42c8131bee04267748b';
const evidencePath = 'docs/evidence/issue-771-repair/ci';
const expectedJobs = [
  { run: 39180639406, job: 117832439061, workflow: 'CI',
    name: 'verify.py (real pytest counts + selftests + capability gates)', step: 'Verify' },
  { run: 39180639803, job: 117832552676, workflow: 'Gate',
    name: 'python ci / python 3.12', step: 'Pytest (unit tests with coverage)' },
];

function verifyRecords(directory) {
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'manifest.json'), 'utf8'));
  assert.equal(manifest.original_head, originalHead, 'wrong original head');
  assert.equal(manifest.repository, 'stranske/Orchestrator', 'wrong repository');
  assert.equal(manifest.records.length, expectedJobs.length, 'incomplete record set');
  const read = (file) => {
    const bytes = fs.readFileSync(path.join(directory, file));
    assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),
      manifest.files[file], `receipt digest mismatch: ${file}`);
    return bytes.toString('utf8');
  };
  return expectedJobs.map(expected => {
    const record = manifest.records.find(r => r.run_id === expected.run);
    assert.ok(record, 'missing original run');
    assert.equal(record.job_id, expected.job, 'wrong original job');
    const run = JSON.parse(read(`run-${expected.run}.json`));
    assert.equal(run.id, expected.run, 'run ID mismatch');
    assert.equal(run.name, expected.workflow, 'workflow mismatch');
    assert.equal(run.head_sha, originalHead, 'run head mismatch');
    assert.equal(run.head_commit.id, originalHead, 'head commit mismatch');
    assert.equal(run.status, 'completed', 'run incomplete');
    assert.equal(run.conclusion, 'success', 'run not successful');
    const jobs = JSON.parse(read(`jobs-${expected.run}.json`)).jobs;
    const job = jobs.find(j => j.id === expected.job);
    assert.ok(job, 'missing original job');
    assert.equal(job.run_id, expected.run, 'job belongs to another run');
    assert.equal(job.name, expected.name, 'job name mismatch');
    assert.equal(job.status, 'completed', 'job incomplete');
    assert.equal(job.conclusion, 'success', 'job not successful');
    const step = job.steps.find(s => s.name === expected.step);
    assert.ok(step, 'missing pytest step');
    assert.equal(step.conclusion, 'success', 'pytest step not successful');

    const raw = read(`job-${expected.job}.log`);
    const lines = raw.replace(/\x1b\[[0-9;]*m/g, '').split('\n')
      .map(line => line.replace(/^\uFEFF?\d{4}-\d{2}-\d{2}T\S+Z /, ''));
    assert.ok(lines.includes(`Complete job name: ${expected.name}`), 'log job mismatch');
    // The first checkout is the consumer. Later checkouts may be Workflows helpers.
    const checkoutIndex = lines.findIndex(line =>
      /^\[command\].*\/git log -1 --format=%H$/.test(line));
    assert.ok(checkoutIndex >= 0, 'missing tested checkout');
    const checkout = lines[checkoutIndex + 1];
    assert.match(checkout, /^[a-f0-9]{40}$/, 'invalid tested checkout');
    assert.equal(record.checkout_sha, checkout, 'checkout annotation mismatch');
    const summaries = lines.filter(line =>
      /^\s*pytest:\s+\d+ passed,/.test(line) || /^=+ \d+ passed[, ].*=+$/.test(line));
    assert.ok(summaries.length > 0, 'missing measured pytest result');
    assert.equal(new Set(summaries).size, 1, 'conflicting pytest results');
    const summary = summaries[0].trim();
    assert.ok(!/\b[1-9]\d* (?:failed|errors?)\b/.test(summary), 'pytest failed');
    const passed = Number(summary.match(/(\d+) passed/)[1]);
    assert.ok(passed > 0, 'no passing tests');
    if (expected.workflow === 'CI') {
      assert.ok(lines.some(line => line.trim() === 'python3 src/verify.py --floor-may-lag'),
        'missing verify command');
    } else {
      assert.ok(lines.includes('pytest -p ci_test_growth "${args[@]}"'),
        'missing pytest command');
      assert.ok(lines.includes('Checking pytest: success'), 'missing pytest success outcome');
    }
    return { workflow: run.name, run_id: run.id, job_id: job.id, run_head: run.head_sha,
      checkout_sha: checkout, exact_head_execution: checkout === run.head_sha,
      pytest_summary: summary, passed, result: step.conclusion };
  });
}

if (require.main === module) {
  try {
    const directory = process.argv[2] || path.resolve(__dirname, '..', evidencePath);
    process.stdout.write(`${JSON.stringify(verifyRecords(directory), null, 2)}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = { verifyRecords };
