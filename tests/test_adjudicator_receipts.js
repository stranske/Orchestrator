'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { gunzipSync } = require('node:zlib');
const { test } = require('node:test');
const { verifyProof, verifyPacket } = require('../scripts/verify_adjudicator_receipts');

const root = path.resolve(__dirname, '..');
const receiptPath = 'docs/verification/issue-428-named-acceptance-20261007';

function run(command, args, options = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-receipt-test-'));
  const out = fs.openSync(path.join(dir, 'stdout'), 'w');
  const err = fs.openSync(path.join(dir, 'stderr'), 'w');
  try {
    const result = spawnSync(command, args, { cwd: root, timeout: 30000,
      ...options, stdio: ['ignore', out, err] });
    assert.ifError(result.error);
    return { status: result.status, stdout: fs.readFileSync(path.join(dir, 'stdout')),
      stderr: fs.readFileSync(path.join(dir, 'stderr')) };
  } finally {
    fs.closeSync(out);
    fs.closeSync(err);
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

function packet() {
  const dir = path.join(root, receiptPath);
  const validation = JSON.parse(fs.readFileSync(path.join(dir, 'validation.json')));
  const packed = JSON.parse(gunzipSync(fs.readFileSync(path.join(dir, 'phase-artifacts.json.gz'))));
  const members = new Map(Object.entries(packed).map(([name, item]) =>
    [name, Buffer.from(item.base64, 'base64')]));
  const readBlob = (revision, file) => {
    const result = run('git', ['show', `${revision}:${file}`]);
    assert.equal(result.status, 0, result.stderr?.toString());
    return result.stdout;
  };
  return { validation, members, readBlob };
}

function check(p) {
  return verifyPacket(p.validation, p.members, p.readBlob);
}

function syncControls(p) {
  p.members.set('controls.json', Buffer.from(JSON.stringify(p.validation.required_mutations)));
}

test('retained named-test faults and restoration bind to evaluated Git bytes without acceptance inflation', () => {
  const result = verifyProof(root);
  assert.equal(result.historical_receipts_valid, true);
  assert.equal(result.focused_suite_passes, 39);
  assert.deepEqual(result.mutations.map((r) => [r.red_failures, r.restored_passes]), [[1, 1], [3, 5]]);
  assert.equal(result.fresh_current_source_run, false);
  assert.equal(result.operational_acceptance_complete, false);
  assert.ok(result.changed_since_evaluation.includes('src/adjudicator_retro.py'));
  assert.ok(result.changed_since_evaluation.includes('tests/test_adjudicator_retro.py'));
});

test('evaluated source hashes cannot be replaced with current source hashes', () => {
  const p = packet();
  p.validation.evaluated_blob_hashes['src/adjudicator_retro.py'] = crypto.createHash('sha256')
    .update(fs.readFileSync(path.join(root, 'src/adjudicator_retro.py'))).digest('hex');
  assert.throws(() => check(p), /evaluated source/);
});

test('claimed source mutation and exact restoration must agree with the recorded bytes', () => {
  for (const field of ['mutated_sha256', 'restored_sha256']) {
    const p = packet();
    p.validation.required_mutations[0][field] = '0'.repeat(64);
    syncControls(p);
    assert.throws(() => check(p), /mutation source digest|restoration is not byte-identical/);
  }
});

test('a failure of a different named command is rejected', () => {
  const p = packet();
  p.validation.required_mutations[0].phases[0].argv[3] = 'tests/test_other.py::test_other';
  syncControls(p);
  assert.throws(() => check(p), /wrong named command/);
});

test('JUnit skips, wrong tests and unrelated failures cannot masquerade as mutation witnesses', () => {
  for (const [before, after, reason] of [
    ['<testcase ', '<testcase ', null],
    ['test_retro_records_verdicts_without_changing_outcomes', 'test_unrelated', /wrong JUnit cases/],
    ['outcomes() == before', 'outcomes() != before', /wrong failure assertion/],
  ]) {
    const p = packet();
    const name = reason ? 'outcomes-red.xml' : 'outcomes-green.xml';
    const xml = p.members.get(name).toString('utf8');
    p.members.set(name, Buffer.from(reason ? xml.replaceAll(before, after) :
      xml.replace(/<testcase\b([^>]*?)\s*\/>/, '<testcase$1><skipped /></testcase>')));
    assert.throws(() => check(p), reason || /skipped\/error\/wrong outcome/);
  }
});

test('suite attributes cannot hide a changed red case outcome', () => {
  const p = packet();
  const name = 'unconditional-offer-red.xml';
  p.members.set(name, Buffer.from(p.members.get(name).toString('utf8')
    .replace('<failure ', '<error ').replace('</failure>', '</error>')));
  assert.throws(() => check(p), /skipped\/error\/wrong outcome/);
});

test('missing native receipts and a focused-suite skip block validation', () => {
  const p = packet();
  p.members.delete('outcomes-red.xml');
  assert.throws(() => check(p), /missing member/);
  const q = packet();
  q.members.set('509-current-main-acceptance.txt', Buffer.from('39 passed, 1 skipped in 2s'));
  assert.throws(() => check(q));
});

test('changed retained bundle fails immutable binding and CLI publishes no success', (t) => {
  const copy = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-receipt-'));
  t.after(() => fs.rmSync(copy, { recursive: true, force: true }));
  // A separate work tree shares only the read-only Git object store.
  fs.writeFileSync(path.join(copy, '.git'), `gitdir: ${path.join(root, '.git')}\n`);
  fs.mkdirSync(path.join(copy, receiptPath), { recursive: true });
  for (const name of ['manifest.json', 'validation.json', 'phase-artifacts.json.gz']) {
    fs.copyFileSync(path.join(root, receiptPath, name), path.join(copy, receiptPath, name));
  }
  fs.appendFileSync(path.join(copy, receiptPath, 'phase-artifacts.json.gz'), 'changed');
  assert.throws(() => verifyProof(copy), /retained bytes differ from revision/);
  const script = path.join(root, 'scripts/verify_adjudicator_receipts.js');
  const result = run(process.execPath, [script], { cwd: root,
    env: { ...process.env, PYTHON: '/absent/python-for-receipt-test' } });
  assert.equal(result.status, 1);
  assert.equal(result.stdout.toString(), '');
});

test('merge verifier failure was a stale floor reconciled on its direct successor with ceilings preserved', () => {
  const dir = path.join(root, 'docs/verification/issue-1074-named-receipts');
  const evidence = JSON.parse(fs.readFileSync(path.join(dir, 'merge-verify.json')));
  const excerpt = fs.readFileSync(path.join(dir, 'merge-verify-excerpt.txt'), 'utf8');
  const read = (revision, file) => {
    const result = run('git', ['show', `${revision}:${file}`]);
    assert.equal(result.status, 0);
    return JSON.parse(result.stdout);
  };
  const before = read(evidence.evaluated_merge, '.verify-floor.json');
  const after = read(evidence.reconciliation.commit, '.verify-floor.json');
  assert.equal(run('git', ['rev-parse', `${evidence.reconciliation.commit}^`]).stdout.toString().trim(),
    evidence.evaluated_merge);
  assert.equal(before.collected, 2592);
  assert.equal(after.collected, 2605);
  assert.equal(after.passed, 2605);
  const ceilings = (floor) => Object.fromEntries(Object.entries(floor)
    .filter(([key]) => key.endsWith('_max')));
  assert.deepEqual(ceilings(after), ceilings(before));
  assert.deepEqual(evidence.observed, { passed: 2579, failed: 0, skipped: 26,
    collected: 2605, recorded_floor: 2592 });
  assert.match(excerpt, /2579 passed, 0 failed, 26\/26 max skipped \(2605 collected; floor 2592/);
  assert.match(excerpt, /floor is BEHIND reality: 2605 collected > floor 2592/);
  assert.match(excerpt, /floor updated: collected=2605 passed=2605 \(ceilings preserved\)/);
  assert.equal(evidence.operational_acceptance_complete, false);
});
