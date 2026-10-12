'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { verifyProof } = require('../scripts/verify_seatbelt_receipts');

const repo = path.resolve(__dirname, '..');
const receiptPath = 'docs/verification/issue-532-seatbelt-regex/native-recovery-20261011';

function copy(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'seatbelt-receipts-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (const file of ['src/adapters.py', 'tests/test_seatbelt_regex_quoting.py']) {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.copyFileSync(path.join(repo, file), path.join(root, file));
  }
  fs.cpSync(path.join(repo, receiptPath), path.join(root, receiptPath), { recursive: true });
  return root;
}

function reviseReceipt(root, name, change) {
  const dir = path.join(root, receiptPath);
  const archive = path.join(dir, 'proof.tar.gz');
  const scratch = path.join(root, 'packet');
  fs.mkdirSync(scratch);
  const extracted = spawnSync('tar', ['-xzf', archive, '-C', scratch]);
  assert.equal(extracted.status, 0, extracted.stderr?.toString());
  const file = path.join(scratch, name);
  fs.writeFileSync(file, change(fs.readFileSync(file, 'utf8')));
  const hashes = JSON.parse(fs.readFileSync(path.join(dir, 'sha256.json'), 'utf8'));
  hashes[name] = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const packed = spawnSync('tar', ['-czf', archive, '-C', scratch, ...fs.readdirSync(scratch)]);
  assert.equal(packed.status, 0, packed.stderr?.toString());
  hashes['proof.tar.gz'] = crypto.createHash('sha256').update(fs.readFileSync(archive)).digest('hex');
  fs.writeFileSync(path.join(dir, 'sha256.json'), JSON.stringify(hashes));
}

test('retained native RED/restoration/GREEN covers the current production and regression bytes', async () => {
  assert.equal((await verifyProof(repo)).native_probes, 5);
});

for (const file of ['src/adapters.py', 'tests/test_seatbelt_regex_quoting.py']) {
  test(`stale native evidence is rejected after changing ${file}`, async (t) => {
    const root = copy(t);
    fs.appendFileSync(path.join(root, file), '\n# changed candidate\n');
    await assert.rejects(verifyProof(root), /native evidence does not cover current/);
  });
}

test('corrupt archive is rejected before interpreting its receipts', async (t) => {
  const root = copy(t);
  fs.appendFileSync(path.join(root, receiptPath, 'proof.tar.gz'), 'corrupt');
  await assert.rejects(verifyProof(root), /archive digest mismatch/);
});

test('restoration claim must be backed by the recorded source reversal digest', async (t) => {
  const root = copy(t);
  reviseReceipt(root, '533-proof-results.json', (text) => {
    const results = JSON.parse(text);
    results.red.sha256 = results.green.sha256;
    return JSON.stringify(results);
  });
  await assert.rejects(verifyProof(root), /RED did not use the recorded production reversal/);
});

test('a GREEN skip cannot stand in for a native pass even with consistent archive hashes', async (t) => {
  const root = copy(t);
  reviseReceipt(root, '533-proof-green.xml', (text) => text.replace(
    /(<testcase\b[^>]*?) \/>/, '$1><skipped message="macOS absent" /></testcase>',
  ));
  await assert.rejects(verifyProof(root), /skipped\/error\/nonpassing case/);
});

test('Python-only GREEN cannot stand in for five actual native probes', async (t) => {
  const root = copy(t);
  reviseReceipt(root, '533-proof-green.log', (text) => text.replace(
    'native Seatbelt probe: OK', 'native acceptance UNKNOWN',
  ));
  await assert.rejects(verifyProof(root));
});
