'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { verifyCoverage, main } = require('../scripts/verify_issue_771_coverage');

const source = path.resolve(__dirname, '../docs/evidence/issue-771-repair');
const sha256 = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'issue-771-coverage-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (const name of ['coverage', 'ci']) {
    fs.mkdirSync(path.join(root, name));
    // Only the direct records are inputs; new audit outputs are not fixtures.
    for (const entry of fs.readdirSync(path.join(source, name), { withFileTypes: true })) {
      if (entry.isFile()) fs.copyFileSync(path.join(source, name, entry.name),
        path.join(root, name, entry.name));
    }
  }
  const directory = path.join(root, 'coverage');
  const recordPath = path.join(directory, 'records.json');
  const records = JSON.parse(fs.readFileSync(recordPath, 'utf8'));
  return { directory, records,
    save() { fs.writeFileSync(recordPath, JSON.stringify(records)); },
    changeLog(transform) {
      const file = records.coverage_job.log;
      const target = path.join(directory, file);
      fs.writeFileSync(target, transform(fs.readFileSync(target, 'utf8')));
      records.files[file] = sha256(fs.readFileSync(target));
      this.save();
    },
    changeJob(transform) {
      const file = 'jobs-39180639406.json';
      const target = path.join(root, 'ci', file);
      const jobs = JSON.parse(fs.readFileSync(target, 'utf8'));
      transform(jobs.jobs.find(j => j.id === 117832439523));
      fs.writeFileSync(target, JSON.stringify(jobs));
      const manifestPath = path.join(root, 'ci', 'manifest.json');
      const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
      manifest.files[file] = sha256(fs.readFileSync(target));
      fs.writeFileSync(manifestPath, JSON.stringify(manifest));
    },
  };
}

test('raw recovered report recalculates statements and keeps unavailable comparisons unknown', () => {
  const result = verifyCoverage(path.join(source, 'coverage'));
  assert.equal(result.observations.covered_statements, 2825 - 226);
  assert.equal(result.observations.reported_statement_percent, 92);
  assert.equal(result.observations.pytest_collected, 5470);
  assert.equal(result.observations.selftests_ran, 100);
  assert.equal(result.exact_head_execution, false);
  assert.equal(result.comparable, false);
  assert.equal(result.exact_test_population, null);
  assert.equal(result.dependency_versions, null);
  for (const key of ['num_branches', 'missing_branches', 'comparable_baseline',
    'missing_statement_delta', 'combined_unit_delta', 'shortfall_against_180_combined_units']) {
    assert.equal(result.observations[key], null);
  }
});

test('unavailable baseline and branch data cannot be filled with invented numeric reductions', t => {
  const f = fixture(t);
  for (const key of ['num_branches', 'missing_branches', 'comparable_baseline',
    'missing_statement_delta', 'combined_unit_delta', 'shortfall_against_180_combined_units']) {
    f.records.observations[key] = 6;
    f.save();
    assert.throws(() => verifyCoverage(f.directory), /unsupported measurement or comparison/);
    f.records.observations[key] = null;
  }
});

test('reported counts must agree with the raw report rather than copied annotations', t => {
  const f = fixture(t);
  for (const key of ['num_statements', 'missing_statements', 'covered_statements',
    'reported_statement_percent', 'pytest_collected', 'selftests_total', 'instrumented_processes']) {
    f.records.observations[key] += 1;
    f.save();
    assert.throws(() => verifyCoverage(f.directory), /observation disagrees with raw log/);
    f.records.observations[key] -= 1;
  }
});

test('corrupt raw logs fail integrity verification', t => {
  const f = fixture(t);
  fs.appendFileSync(path.join(f.directory, f.records.coverage_job.log), 'changed');
  assert.throws(() => verifyCoverage(f.directory), /receipt digest mismatch/);
});

test('rehashed configuration drift still fails the historical Git blob identity', t => {
  const f = fixture(t);
  const file = 'historical-pyproject.toml.txt';
  const target = path.join(f.directory, file);
  fs.writeFileSync(target, fs.readFileSync(target, 'utf8').replace('branch = false', 'branch = true'));
  f.records.files[file] = sha256(fs.readFileSync(target));
  f.save();
  assert.throws(() => verifyCoverage(f.directory), /historical configuration blob mismatch/);
});

test('matching file counts cannot hide a missing configuration source', t => {
  const f = fixture(t);
  f.records.configuration_snapshots[1] = f.records.configuration_snapshots[0];
  f.save();
  assert.throws(() => verifyCoverage(f.directory), /wrong configuration population/);
});

test('baseline source inspection must refer to the requested baseline and succeed', t => {
  const f = fixture(t);
  f.records.configuration_snapshots[0].baseline_command[2] = 'main:pyproject.toml';
  f.save();
  assert.throws(() => verifyCoverage(f.directory), /wrong baseline inspection command/);
  f.records.configuration_snapshots[0].baseline_exit_code = 128;
  f.save();
  assert.throws(() => verifyCoverage(f.directory), /baseline inspection failed/);
});

test('incorrect report percentages and pytest population totals are rejected even after rehashing', t => {
  const f = fixture(t);
  f.changeLog(log => log.replace('2825    226  92.0%', '2825    226  93.0%'));
  assert.throws(() => verifyCoverage(f.directory), /percentage disagrees with counts/);
  f.changeLog(log => log.replace('2825    226  93.0%', '2825    226  92.0%')
    .replace('5443 passed, 0 failed', '5444 passed, 0 failed'));
  assert.throws(() => verifyCoverage(f.directory), /pytest population totals disagree/);
});

test('a missing or ambiguous feedback report cannot stand in for one measurement', t => {
  const f = fixture(t);
  const original = fs.readFileSync(path.join(f.directory, f.records.coverage_job.log), 'utf8');
  f.changeLog(log => log + '\nsrc/feedback.py                         2825    226  92.0%\n');
  assert.throws(() => verifyCoverage(f.directory), /missing or ambiguous measurement/);
  f.changeLog(() => original.replace(/^.*src\/feedback\.py.*\n/m, ''));
  assert.throws(() => verifyCoverage(f.directory), /missing or ambiguous measurement/);
});

test('the measured checkout cannot be relabeled as original-head execution', t => {
  const f = fixture(t);
  f.records.observed_checkout_sha = f.records.head_commit;
  f.save();
  assert.throws(() => verifyCoverage(f.directory), /checkout annotation mismatch/);
});

test('successful job metadata cannot hide a failed coverage measurement step', t => {
  const f = fixture(t);
  f.changeJob(job => {
    job.steps.find(s => s.name === 'Measure combined coverage').conclusion = 'failure';
  });
  assert.throws(() => verifyCoverage(f.directory), /coverage measurement step unsuccessful/);
});

test('a failing pytest result or a numeric exit invented from successful metadata is rejected', t => {
  const f = fixture(t);
  f.records.coverage_job.process_exit_code = 0;
  f.save();
  assert.throws(() => verifyCoverage(f.directory), /numeric process exit was not preserved/);
  f.records.coverage_job.process_exit_code = null;
  f.changeLog(log => log.replace('5443 passed, 0 failed', '5443 passed, 1 failed'));
  assert.throws(() => verifyCoverage(f.directory), /pytest failed/);
});

test('CLI prints a recovered-history audit and returns a failure for absent evidence', t => {
  let stdout = '';
  let stderr = '';
  const out = text => { stdout += text; };
  const err = text => { stderr += text; };
  assert.equal(main([], out, err), 0, stderr);
  assert.equal(JSON.parse(stdout).comparable, false);
  assert.equal(stderr, '');
  const f = fixture(t);
  stdout = '';
  assert.equal(main([path.join(f.directory, 'absent')], out, err), 1);
  assert.match(stderr, /ENOENT/);
  assert.equal(stdout, '');
  stderr = '';
  assert.equal(main(['one', 'two'], out, err), 1);
  assert.match(stderr, /usage:/);
});
