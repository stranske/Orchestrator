'use strict';

// Qualify the recovered #582 mutation receipt against merged Git bytes. This
// does not rerun its tests or authenticate its author, storage dates, or claims.
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { isDeepStrictEqual } = require('node:util');

const cases = [
  {
    name: 'daily-cap', file: 'src/redirect_apply.py',
    node: 'tests/test_redirect_apply_prescreen.py::test_redirect_apply_daily_cap_refuses_second_apply',
    mutation: 'disable applies_today guard',
  },
  {
    name: 'fixture-count', file: 'tests/fixtures/redirect_stalls/historical-12.json',
    node: 'tests/test_redirect_chain_gate.py::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine',
    mutation: 'remove historical-12.json',
  },
];
const sha256 = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');

function checkReceipt({ repository, receiptPath }) {
  const result = {
    observed_at: new Date().toISOString(), status: 'blocked', byte_bound_cases: 0, bindings: [],
    execution_date: 'not_recorded', recorded_test_outcomes: 'unverified_claims',
    source_to_fixture_bindings: 'not_established', historical_replay_receipt: 'not_established',
    operational_acceptance: 'not_established',
  };
  let bytes;
  let receipt;
  try {
    bytes = fs.readFileSync(receiptPath);
    receipt = JSON.parse(bytes.toString('utf8'));
  } catch {
    result.reason = 'receipt_unavailable_or_invalid';
    return result;
  }
  result.receipt_sha256 = sha256(bytes);
  const valid = receipt && /^[a-f0-9]{40}$/.test(receipt.evaluated_sha || '')
    && Array.isArray(receipt.results) && receipt.results.length === cases.length
    && cases.every((expected) => {
      const rows = receipt.results.filter((row) => row?.case === expected.name);
      if (rows.length !== 1) return false;
      const row = rows[0];
      return row.evaluated_sha === receipt.evaluated_sha
        && /^[a-f0-9]{64}$/.test(row.before_sha256 || '')
        && row.before_sha256 === row.restored_sha256
        && row.red_returncode === 1 && row.green_returncode === 0
        && row.red_assertion === true && row.restored_identical === true
        && row.mutation === expected.mutation
        && isDeepStrictEqual(row.argv, ['python3', '-m', 'pytest', expected.node, '-q']);
    });
  if (!valid) {
    result.reason = 'receipt_claims_invalid';
    return result;
  }
  result.evaluated_sha = receipt.evaluated_sha;
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-receipt-git-'));
  try {
    // File descriptors avoid this runner's observed EPERM on captured pipes.
    // Every process error still blocks; no error is discarded to credit bytes.
    const git = (args) => {
      const output = path.join(scratch, 'stdout');
      const fd = fs.openSync(output, 'w');
      try {
        const run = spawnSync('git', ['-C', repository, ...args], {
          stdio: ['ignore', fd, 'ignore'], timeout: 30000,
        });
        return run.status === 0 && !run.error && !run.signal ? fs.readFileSync(output) : null;
      } finally {
        fs.closeSync(fd);
      }
    };
    const main = git(['rev-parse', '--verify', 'refs/remotes/origin/main^{commit}']);
    if (!main || !/^[a-f0-9]{40}$/.test(main.toString().trim())) {
      result.reason = 'merged_main_reference_unavailable';
      return result;
    }
    result.merged_main_sha = main.toString().trim();
    if (!git(['merge-base', '--is-ancestor', receipt.evaluated_sha, result.merged_main_sha])) {
      result.reason = 'revision_not_on_merged_main';
      return result;
    }
    const bindings = [];
    for (const expected of cases) {
      const source = git(['show', `${receipt.evaluated_sha}:${expected.file}`]);
      const row = receipt.results.find((item) => item.case === expected.name);
      if (!source || sha256(source) !== row.before_sha256) {
        result.reason = 'merged_bytes_differ';
        return result;
      }
      bindings.push({ case: expected.name, file: expected.file, merged_blob_sha256: sha256(source) });
    }
    result.bindings = bindings;
    result.byte_bound_cases = bindings.length;
    result.status = 'bytes_verified';
    return result;
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

function main(args = process.argv.slice(2)) {
  if (args.length !== 1 || args[0].startsWith('--')) {
    console.error('Usage: node scripts/check_redirect_recovered_receipt.js <recovered-receipt.json>');
    return 2;
  }
  try {
    const result = checkReceipt({ repository: path.resolve(__dirname, '..'), receiptPath: args[0] });
    console.log(JSON.stringify(result, null, 2));
    return result.status === 'bytes_verified' ? 0 : 1;
  } catch {
    console.error('Receipt byte verification unavailable; historical execution remains unverified.');
    return 1;
  }
}

if (require.main === module) process.exitCode = main();
module.exports = { checkReceipt, main };
