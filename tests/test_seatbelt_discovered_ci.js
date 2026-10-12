'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { verifyDiscoveredCI, verifyHostedCI } = require('../scripts/verify_seatbelt_receipts');

const snapshot = JSON.parse(fs.readFileSync(path.join(__dirname,
  '../docs/verification/issue-532-seatbelt-regex/0f88580-ci-discovery.json'), 'utf8'));
const head = snapshot.head_sha;
const copy = () => structuredClone(snapshot);

test('bounded discovery recovers both measured tree shapes and keeps full acceptance UNKNOWN', () => {
  const result = verifyDiscoveredCI(snapshot, head);
  assert.deepEqual(result.measurements.map(m => [m.passed, m.skipped, m.collected]),
    [[2863, 31, 2894], [2842, 52, 2894]]);
  assert.ok(result.measurements.every(m => m.selftests === 97 && m.gates_green === 3));
  assert.match(result.complete_topology, /^UNKNOWN:/);
  assert.match(result.hosted_acceptance, /^UNKNOWN:/);
  assert.throws(() => verifyHostedCI(snapshot, head), /seatbelt-hosted-ci/);
});

test('recovered measurements cannot accept a successor head', () => {
  assert.throws(() => verifyDiscoveredCI(snapshot, 'a'.repeat(40)), /different head/);
});

test('a wrong checkout cannot supply measurements even if metadata names the candidate', () => {
  const evidence = copy();
  evidence.receipts[0].selected_authentic_lines = evidence.receipts[0].selected_authentic_lines
    .map(line => line.replace(head, 'a'.repeat(40)));
  assert.throws(() => verifyDiscoveredCI(evidence, head), /checkout head/);
});

test('a missing flat-copy job cannot be masked by checkout success', () => {
  const evidence = copy();
  evidence.runs[0].jobs = evidence.runs[0].jobs.filter(job => !job.name.includes('flat exec'));
  assert.throws(() => verifyDiscoveredCI(evidence, head), /missing.*product job/);
});

test('missing, duplicate and unrelated measurement receipts are rejected', () => {
  for (const change of [
    e => e.receipts.pop(),
    e => e.receipts.push(structuredClone(e.receipts[0])),
    e => e.receipts.push({ job_id: 1, selected_authentic_lines: [] }),
  ]) {
    const evidence = copy();
    change(evidence);
    assert.throws(() => verifyDiscoveredCI(evidence, head), /measurement receipt/);
  }
});

test('failed observed jobs cannot be hidden by bounded discovery', () => {
  const evidence = copy();
  evidence.runs[1].jobs[0].conclusion = 'failure';
  assert.throws(() => verifyDiscoveredCI(evidence, head), /failed or unfinished/);
});

test('job records must belong to the discovered run and have distinct identities', () => {
  const evidence = copy();
  evidence.runs[0].jobs[0].run_id += 1;
  assert.throws(() => verifyDiscoveredCI(evidence, head), /job run mismatch/);
  const duplicate = copy();
  duplicate.runs[0].jobs.push(structuredClone(duplicate.runs[0].jobs[0]));
  assert.throws(() => verifyDiscoveredCI(duplicate, head), /duplicate job identity/);
});

test('successful setup does not replace skipped or absent product steps', () => {
  for (const change of [
    e => { e.runs[0].jobs[0].product_steps[0].conclusion = 'skipped'; },
    e => { e.runs[0].jobs[0].product_steps = []; },
  ]) {
    const evidence = copy();
    change(evidence);
    assert.throws(() => verifyDiscoveredCI(evidence, head), /product step/);
  }
});

test('a truncated selftest transcript cannot establish measured full verification', () => {
  const evidence = copy();
  evidence.receipts[0].selected_authentic_lines = evidence.receipts[0].selected_authentic_lines
    .filter(line => !line.includes('selftests:'));
  assert.throws(() => verifyDiscoveredCI(evidence, head), /missing selftest measurement/);
});

test('selftest ceilings and roster counts must agree with the verdict', () => {
  for (const [before, after, error] of [
    ['97 of 104', '96 of 104', /invalid selftest measurement/],
    ['7/7 max', '7/6 max', /invalid selftest measurement/],
    ['97 selftests spoke', '96 selftests spoke', /selftest count mismatch/],
    ['2863 tests actually executed', '2862 tests actually executed', /test count mismatch/],
    ['3 of 5 gates green', '0 of 5 gates green', /invalid gate measurement/],
  ]) {
    const evidence = copy();
    evidence.receipts[0].selected_authentic_lines = evidence.receipts[0].selected_authentic_lines
      .map(line => line.replace(before, after));
    assert.throws(() => verifyDiscoveredCI(evidence, head), error);
  }
});

test('discovery limitations must be retained and cannot be erased by successful measurements', () => {
  const evidence = copy();
  delete evidence.discovery_limit;
  assert.throws(() => verifyDiscoveredCI(evidence, head), /discovery limitation/);
  const claimedComplete = copy();
  claimedComplete.discovery_limit = 'complete';
  assert.match(verifyDiscoveredCI(claimedComplete, head).complete_topology, /^UNKNOWN:/);
});

test('a successful Gate cannot compensate for a skipped measured CI product job', () => {
  const evidence = copy();
  evidence.runs[0].jobs[0].conclusion = 'skipped';
  assert.throws(() => verifyDiscoveredCI(evidence, head), /product job did not execute/);
});
