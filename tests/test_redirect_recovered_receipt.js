'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { checkReceipt, main } = require('../scripts/check_redirect_recovered_receipt');

const repository = path.resolve(__dirname, '..');
const receiptPath = path.join(repository,
  'docs/verification/issue-1088-recovered-october14-recheck.json');

function control(t, change) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-receipt-control-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const receipt = JSON.parse(fs.readFileSync(receiptPath));
  change(receipt);
  const file = path.join(root, 'receipt.json');
  fs.writeFileSync(file, JSON.stringify(receipt));
  return file;
}

test('recovered October 14 claims bind to merged bytes without historical execution credit', () => {
  const before = fs.readFileSync(receiptPath);
  const result = checkReceipt({ repository, receiptPath });
  assert.equal(result.status, 'bytes_verified', JSON.stringify(result));
  assert.equal(result.byte_bound_cases, 2);
  assert.equal(result.evaluated_sha, '81fbab57e6622cd67c28dd7b7b3507a8d3ff55c0');
  assert.match(result.receipt_sha256, /^[a-f0-9]{64}$/);
  assert.match(result.merged_main_sha, /^[a-f0-9]{40}$/);
  assert.deepEqual(result.bindings.map((row) => row.file), [
    'src/redirect_apply.py', 'tests/fixtures/redirect_stalls/historical-12.json',
  ]);
  assert.ok(result.bindings.every((row) => /^[a-f0-9]{64}$/.test(row.merged_blob_sha256)));
  assert.equal(result.execution_date, 'not_recorded');
  assert.equal(result.recorded_test_outcomes, 'unverified_claims');
  assert.equal(result.source_to_fixture_bindings, 'not_established');
  assert.equal(result.historical_replay_receipt, 'not_established');
  assert.equal(result.operational_acceptance, 'not_established');
  assert.deepEqual(fs.readFileSync(receiptPath), before);
});

test('duplicate, missing, or contradictory mutation claims cannot bind', (t) => {
  const changes = [
    (doc) => doc.results.pop(),
    (doc) => { doc.results[1] = structuredClone(doc.results[0]); },
    (doc) => { doc.results[0].evaluated_sha = 'a'.repeat(40); },
    (doc) => { doc.results[0].restored_sha256 = 'b'.repeat(64); },
    (doc) => { doc.results[0].red_returncode = 0; },
    (doc) => { doc.results[0].green_returncode = 1; },
    (doc) => { doc.results[0].red_assertion = false; },
    (doc) => { doc.results[0].restored_identical = false; },
    (doc) => { doc.results[0].argv[3] = 'tests/test_other.py::test_other'; },
    (doc) => { doc.results[0].mutation = 'unrelated mutation'; },
  ];
  for (const change of changes) {
    const result = checkReceipt({ repository, receiptPath: control(t, change) });
    assert.equal(result.status, 'blocked');
    assert.equal(result.byte_bound_cases, 0);
    assert.equal(result.reason, 'receipt_claims_invalid');
  }
});

test('matching restoration claims must also match the merged source bytes', (t) => {
  const file = control(t, (doc) => {
    doc.results[0].before_sha256 = 'b'.repeat(64);
    doc.results[0].restored_sha256 = 'b'.repeat(64);
  });
  const result = checkReceipt({ repository, receiptPath: file });
  assert.equal(result.status, 'blocked');
  assert.equal(result.reason, 'merged_bytes_differ');
  assert.equal(result.byte_bound_cases, 0);
});

test('missing repositories, unknown revisions and malformed receipts stay blocked and sanitized', (t) => {
  const file = control(t, (doc) => {
    doc.evaluated_sha = 'a'.repeat(40);
    for (const row of doc.results) row.evaluated_sha = doc.evaluated_sha;
  });
  assert.equal(checkReceipt({ repository, receiptPath: file }).reason,
    'revision_not_on_merged_main');
  assert.equal(checkReceipt({ repository: path.dirname(file), receiptPath }).reason,
    'merged_main_reference_unavailable');
  fs.writeFileSync(file, 'PRIVATE-missing-or-malformed');
  for (const candidate of [file, `${file}.missing`]) {
    const result = checkReceipt({ repository, receiptPath: candidate });
    assert.equal(result.status, 'blocked');
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE-|receipt-control-/);
  }
});

test('CLI requires a receipt and reports byte verification separately from execution', (t) => {
  const stdout = [];
  const stderr = [];
  t.mock.method(console, 'log', (value) => stdout.push(value));
  t.mock.method(console, 'error', (value) => stderr.push(value));
  assert.equal(main([]), 2);
  assert.equal(main([receiptPath]), 0);
  assert.equal(JSON.parse(stdout.at(-1)).status, 'bytes_verified');
  assert.match(stderr.join(), /Usage:/);
});
