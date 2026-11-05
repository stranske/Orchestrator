'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { verifyRecords } = require('../scripts/verify_issue_771_ci');

const evidence = path.resolve(__dirname, '../docs/evidence/issue-771-repair/ci');

function copy(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'issue-771-ci-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  fs.cpSync(evidence, directory, { recursive: true });
  return directory;
}

function revise(directory, file, change) {
  const target = path.join(directory, file);
  fs.writeFileSync(target, change(fs.readFileSync(target, 'utf8')));
  // Re-hash so semantic tests exercise the evidence checks, beyond corruption detection.
  const manifestPath = path.join(directory, 'manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  manifest.files[file] = crypto.createHash('sha256').update(fs.readFileSync(target)).digest('hex');
  fs.writeFileSync(manifestPath, JSON.stringify(manifest));
}

test('recovered CI and Gate logs expose passing results and the actual merge checkout', () => {
  const records = verifyRecords(evidence);
  assert.deepEqual(records.map(r => r.passed), [5443, 5429]);
  assert.ok(records.every(r => r.result === 'success'));
  assert.ok(records.every(r => r.run_head === '18ad97a054dab7f6039dd42c8131bee04267748b'));
  assert.ok(records.every(r => r.checkout_sha === '12c5c58517863f747972538c9bb17c568bc0bf49'));
  assert.ok(records.every(r => r.exact_head_execution === false));
});

test('successful check metadata cannot replace measured pytest output', t => {
  const directory = copy(t);
  revise(directory, 'job-117832439061.log', text => text.replace(/^.*pytest:.*\n/gm, ''));
  assert.throws(() => verifyRecords(directory), /missing measured pytest result/);
});

test('raw log corruption fails the retained digest', t => {
  const directory = copy(t);
  fs.appendFileSync(path.join(directory, 'job-117832552676.log'), 'changed\n');
  assert.throws(() => verifyRecords(directory), /receipt digest mismatch/);
});

test('a different run head is rejected even if the metadata is rehashed', t => {
  const directory = copy(t);
  revise(directory, 'run-39180639406.json', text => {
    const run = JSON.parse(text);
    run.head_sha = 'a'.repeat(40);
    return JSON.stringify(run);
  });
  assert.throws(() => verifyRecords(directory), /run head mismatch/);
});

test('a successful unrelated job cannot stand in for the original pytest job', t => {
  const directory = copy(t);
  revise(directory, 'jobs-39180639803.json', text => {
    const data = JSON.parse(text);
    data.jobs.find(j => j.id === 117832552676).run_id = 1;
    return JSON.stringify(data);
  });
  assert.throws(() => verifyRecords(directory), /job belongs to another run/);
});

test('failed pytest counts cannot be hidden behind successful job metadata', t => {
  const directory = copy(t);
  revise(directory, 'job-117832439061.log', text => text.replace('0 failed, 27/31', '1 failed, 27/31'));
  assert.throws(() => verifyRecords(directory), /pytest failed/);
});

test('a failing pytest step is rejected independently of the job conclusion', t => {
  const directory = copy(t);
  revise(directory, 'jobs-39180639803.json', text => {
    const data = JSON.parse(text);
    data.jobs.find(j => j.id === 117832552676).steps
      .find(s => s.name === 'Pytest (unit tests with coverage)').conclusion = 'failure';
    return JSON.stringify(data);
  });
  assert.throws(() => verifyRecords(directory), /pytest step not successful/);
});

test('a merge checkout cannot be relabeled as exact-head execution', t => {
  const directory = copy(t);
  const target = path.join(directory, 'manifest.json');
  const manifest = JSON.parse(fs.readFileSync(target, 'utf8'));
  manifest.records[0].checkout_sha = manifest.original_head;
  fs.writeFileSync(target, JSON.stringify(manifest));
  assert.throws(() => verifyRecords(directory), /checkout annotation mismatch/);
});

test('the first consumer checkout remains distinct from a later helper checkout', () => {
  assert.notEqual(verifyRecords(evidence)[1].checkout_sha,
    'e9b9424bbae233f44dca3a0b81faaee985372864');
});
