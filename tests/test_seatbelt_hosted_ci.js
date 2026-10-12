'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { verifyHostedCI } = require('../scripts/verify_seatbelt_receipts');
const { withStepLogs } = require('./seatbelt_ci_fixture');

const retained = JSON.parse(fs.readFileSync(path.join(__dirname,
  '../docs/verification/issue-532-seatbelt-regex/380c160-hosted-ci.json'), 'utf8'));
const snapshot = withStepLogs(retained);
const head = snapshot.head_sha;
const copy = () => structuredClone(snapshot);

test('synthetic step-bound hosted CI has executed product checks on the recorded exact head', () => {
  assert.equal(verifyHostedCI(snapshot, head).head_sha, head);
});

test('historical whole-job excerpts lack authenticated producer bindings', () => {
  assert.throws(() => verifyHostedCI(retained, head), /producer binding/);
});

test('hosted success for an older head cannot accept a new candidate', () => {
  assert.throws(() => verifyHostedCI(snapshot, 'a'.repeat(40)), /head/);
});

test('all-skipped jobs cannot accept product CI even under successful workflow conclusions', () => {
  const evidence = copy();
  for (const { jobs } of evidence.runs) {
    for (const job of jobs.jobs) job.conclusion = 'skipped';
  }
  assert.throws(() => verifyHostedCI(evidence, head), /did not execute/);
});

test('successful setup cannot replace a skipped product test step', () => {
  const evidence = copy();
  const job = evidence.runs[1].jobs.jobs.find(j => j.name === 'python ci / python 3.12');
  job.steps.find(s => s.name === 'Pytest (unit tests with coverage)').conclusion = 'skipped';
  assert.throws(() => verifyHostedCI(evidence, head), /step did not execute/);
});

test('bounded job discovery cannot prove complete hosted CI', () => {
  const evidence = copy();
  evidence.runs[1].jobs.jobs.pop();
  assert.throws(() => verifyHostedCI(evidence, head), /incomplete job/);
});

test('a missing matrix member cannot be hidden by internally consistent totals', () => {
  const evidence = copy();
  const jobs = evidence.runs[1].jobs;
  jobs.jobs = jobs.jobs.filter(j => j.name !== 'python ci / python 3.13');
  jobs.total_count = jobs.jobs.length;
  assert.throws(() => verifyHostedCI(evidence, head), /missing.*python 3.13/);
});

test('a failed optional job still prevents a complete CI claim', () => {
  const evidence = copy();
  evidence.runs[1].jobs.jobs.find(j => j.name === 'docker smoke').conclusion = 'failure';
  assert.throws(() => verifyHostedCI(evidence, head), /failed or unfinished/);
});

test('job receipts from a different run attempt are rejected', () => {
  const evidence = copy();
  evidence.runs[0].jobs.jobs[0].run_attempt += 1;
  assert.throws(() => verifyHostedCI(evidence, head), /attempt/);
});

test('a GREEN workflow without measured test counts cannot establish full verification', () => {
  const evidence = copy();
  evidence.receipts[0].selected_authentic_lines = [];
  assert.throws(() => verifyHostedCI(evidence, head), /measurement/);
});

test('a duplicate job cannot stand in for undiscovered checks', () => {
  const evidence = copy();
  const jobs = evidence.runs[0].jobs;
  jobs.jobs.push(structuredClone(jobs.jobs[0]));
  jobs.total_count = jobs.jobs.length;
  assert.throws(() => verifyHostedCI(evidence, head), /duplicate job/);
});

test('a checkout of another candidate cannot supply the measured verifier result', () => {
  const evidence = copy();
  evidence.receipts[0].selected_authentic_lines = evidence.receipts[0].selected_authentic_lines
    .map(line => line.replace(head, 'a'.repeat(40)));
  assert.throws(() => verifyHostedCI(evidence, head), /checkout head/);
});

test('zero executed tests cannot establish measured verification', () => {
  const evidence = copy();
  evidence.receipts[0].selected_authentic_lines = evidence.receipts[0].selected_authentic_lines
    .map(line => line.replace('2863 passed, 0 failed', '0 passed, 0 failed'));
  assert.throws(() => verifyHostedCI(evidence, head), /invalid test measurement/);
});

test('a verifier result exceeding its skip ceiling is rejected', () => {
  const evidence = copy();
  evidence.receipts[0].selected_authentic_lines = evidence.receipts[0].selected_authentic_lines
    .map(line => line.replace('31/31 max', '31/30 max'));
  assert.throws(() => verifyHostedCI(evidence, head), /invalid test measurement/);
});
