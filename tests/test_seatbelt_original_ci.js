'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { verifyOriginalCI } = require('../scripts/verify_seatbelt_receipts');

const repo = path.resolve(__dirname, '..');
const receiptPath = 'docs/verification/issue-532-seatbelt-regex';
const recoveryPath = 'original-ci-recovery-20261011';

function copy(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'seatbelt-original-ci-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const dir = path.join(root, receiptPath);
  fs.mkdirSync(dir, { recursive: true });
  fs.copyFileSync(path.join(repo, receiptPath, 'inherited-ci.json'),
    path.join(dir, 'inherited-ci.json'));
  fs.cpSync(path.join(repo, receiptPath, recoveryPath), path.join(dir, recoveryPath),
    { recursive: true });
  return root;
}

function reviseJSON(file, change) {
  const value = JSON.parse(fs.readFileSync(file, 'utf8'));
  change(value);
  fs.writeFileSync(file, JSON.stringify(value));
}

function manifest(root, change) {
  reviseJSON(path.join(root, receiptPath, recoveryPath, 'manifest.json'), change);
}

function reviseExtraction(root, change) {
  const file = path.join(root, receiptPath, recoveryPath, 'original-boundary-cases.xml');
  fs.writeFileSync(file, change(fs.readFileSync(file, 'utf8')));
  manifest(root, m => {
    m.files['original-boundary-cases.xml'] = crypto.createHash('sha256')
      .update(fs.readFileSync(file)).digest('hex');
  });
}

test('original artifact contains twelve passing boundary cases and measured full-run counts', async () => {
  const result = await verifyOriginalCI(repo);
  assert.equal(result.original_head, '2c4509df9b53a32b4eb4143d7ba396808e38b49a');
  assert.equal(result.focused_passes, 12);
  assert.equal(result.passed, 2717);
  assert.equal(result.skipped, 26);
  assert.equal(result.collected, 2743);
  // The authentic JUnit summary disagrees with its actual case records; expose both.
  assert.deepEqual(result.junit_reported_tests, [2849]);
  assert.match(result.focused_command_transcript, /^UNKNOWN:/);
});

test('original receipts cannot be relabeled as a different head', async (t) => {
  const root = copy(t);
  manifest(root, m => { m.original_head = 'a'.repeat(40); });
  await assert.rejects(verifyOriginalCI(root), /original head mismatch/);
});

test('a different artifact or workflow run cannot replace the recovered original', async (t) => {
  const root = copy(t);
  manifest(root, m => { m.run_id += 1; });
  await assert.rejects(verifyOriginalCI(root), /wrong original run/);
  manifest(root, m => { m.run_id -= 1; m.artifact_id += 1; });
  await assert.rejects(verifyOriginalCI(root), /wrong original artifact/);
});

test('corrupt original archive fails its recorded digest', async (t) => {
  const root = copy(t);
  fs.appendFileSync(path.join(root, receiptPath, recoveryPath, 'gate-coverage-3.12-1.zip'),
    'corrupt');
  await assert.rejects(verifyOriginalCI(root), /receipt digest mismatch/);
});

test('a bounded manifest cannot omit the original focused extraction', async (t) => {
  const root = copy(t);
  manifest(root, m => { delete m.files['original-boundary-cases.xml']; });
  await assert.rejects(verifyOriginalCI(root), /incomplete original receipt manifest/);
});

test('the original JUnit digest must match the actual archive member', async (t) => {
  const root = copy(t);
  manifest(root, m => { m.original_junit_sha256 = '0'.repeat(64); });
  await assert.rejects(verifyOriginalCI(root), /original JUnit digest mismatch/);
});

test('full-run counts must come from actual case records', async (t) => {
  const root = copy(t);
  reviseJSON(path.join(root, receiptPath, 'inherited-ci.json'), m => {
    const job = m.jobs.find(j => j.run_id === 38176809124);
    job.excerpts = job.excerpts.map(line => line.replace('2717 passed', '2823 passed'));
  });
  await assert.rejects(verifyOriginalCI(root), /original pass count mismatch/);
});

test('a missing original focused case cannot be hidden by updating its extraction digest', async (t) => {
  const root = copy(t);
  reviseExtraction(root, text => text.replace(/\s*<testcase[^>]*test_no_boundary_changes_nothing[^>]*\/>/, ''));
  await assert.rejects(verifyOriginalCI(root), /extracted cases differ/);
});

test('a forged focused skip cannot stand in for the archived original pass', async (t) => {
  const root = copy(t);
  reviseExtraction(root, text => text.replace(/(<testcase\b[^>]*?) \/>/,
    '$1><skipped message="missing prerequisite" /></testcase>'));
  await assert.rejects(verifyOriginalCI(root), /extracted cases differ/);
});

test('changing an original case timing also breaks the unchanged-record extraction claim', async (t) => {
  const root = copy(t);
  reviseExtraction(root, text => text.replace('time="0.284"', 'time="0.001"'));
  await assert.rejects(verifyOriginalCI(root), /extracted cases differ/);
});
