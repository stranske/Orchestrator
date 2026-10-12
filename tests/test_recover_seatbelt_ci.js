'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { recoverSeatbeltCI } = require('../scripts/recover_seatbelt_ci');
const { verifyHostedCI } = require('../scripts/verify_seatbelt_receipts');

const snapshot = JSON.parse(fs.readFileSync(path.join(__dirname,
  '../docs/verification/issue-532-seatbelt-regex/380c160-hosted-ci.json'), 'utf8'));
const current = JSON.parse(fs.readFileSync(path.join(__dirname,
  '../docs/verification/issue-532-seatbelt-regex/c49b250-hosted-ci.json'), 'utf8'));

function harness(input = snapshot) {
  const evidence = structuredClone(input);
  const calls = [];
  const options = { expectedHead: evidence.head_sha,
    runIDs: evidence.runs.map(item => item.run.id), pageSize: 2,
    fetchJSON: async endpoint => {
      calls.push(endpoint);
      const match = /\/runs\/(\d+)(?:\/attempts\/(\d+)\/jobs\?per_page=(\d+)&page=(\d+))?$/.exec(endpoint);
      assert.ok(match, `unexpected request: ${endpoint}`);
      const entry = evidence.runs.find(item => item.run.id === Number(match[1]));
      if (!match[2]) return structuredClone(entry.run);
      assert.equal(Number(match[2]), entry.run.run_attempt);
      const start = (Number(match[4]) - 1) * Number(match[3]);
      return { total_count: entry.jobs.total_count,
        jobs: structuredClone(entry.jobs.jobs.slice(start, start + Number(match[3]))) };
    },
    fetchLog: async id => evidence.receipts.find(receipt => receipt.job_id === id)
      .selected_authentic_lines.join('\n'),
  };
  return { evidence, calls, options };
}

test('recovery follows all attempt-specific pages and retains measured exact-head CI', async () => {
  const { calls, options } = harness();
  const recovered = await recoverSeatbeltCI(options);
  assert.deepEqual(recovered.runs, snapshot.runs);
  assert.equal(verifyHostedCI(recovered, options.expectedHead).measurements.length, 2);
  assert.ok(calls.some(endpoint => endpoint.endsWith('&page=6')));
  assert.ok(calls.filter(endpoint => endpoint.includes('/jobs?'))
    .every(endpoint => endpoint.includes('/attempts/1/')));
});

test('authentic current-head REST recovery retains all fifteen jobs without accepting skipped Gate', async () => {
  const { options } = harness(current);
  const recovered = await recoverSeatbeltCI(options);
  assert.deepEqual(recovered, current);
  assert.deepEqual(recovered.runs.map(entry => entry.jobs.total_count), [4, 11]);
  assert.match(recovered.receipts[0].selected_authentic_lines.join('\n'),
    /2877 passed, 0 failed, 26\/31 max skipped \(2903 collected/);
  assert.match(recovered.receipts[1].selected_authentic_lines.join('\n'),
    /2856 passed, 0 failed, 47\/52 max.*skipped \(2903 collected/);
  assert.throws(() => verifyHostedCI(recovered, current.head_sha), /missing.*python 3.12/);
});

test('a skipped Gate Python matrix stays skipped after complete recovery', async () => {
  const { evidence, options } = harness();
  const gate = evidence.runs[1].jobs;
  gate.jobs = gate.jobs.filter(job => !job.name.startsWith('python ci /'));
  gate.jobs.push({ id: 123456, name: 'python ci', run_id: evidence.runs[1].run.id,
    run_attempt: 1, head_sha: evidence.head_sha, status: 'completed',
    conclusion: 'skipped', steps: [] });
  gate.total_count = gate.jobs.length;
  const recovered = await recoverSeatbeltCI(options);
  assert.equal(recovered.runs[1].jobs.jobs.at(-1).conclusion, 'skipped');
  assert.throws(() => verifyHostedCI(recovered, evidence.head_sha), /missing.*product job/);
});

test('empty or truncated later pages cannot produce a complete receipt', async () => {
  for (const count of [0, 1]) {
    const { options } = harness();
    const original = options.fetchJSON;
    options.fetchJSON = async endpoint => {
      const value = await original(endpoint);
      if (endpoint.endsWith('&page=2')) value.jobs = value.jobs.slice(0, count);
      return value;
    };
    await assert.rejects(recoverSeatbeltCI(options), /incomplete job page|truncated job page/);
  }
});

test('missing totals and totals changing between pages are rejected', async () => {
  for (const change of [value => { delete value.total_count; },
    value => { value.total_count += 1; }]) {
    const { options } = harness();
    const original = options.fetchJSON;
    options.fetchJSON = async endpoint => {
      const value = await original(endpoint);
      if (endpoint.endsWith('&page=2')) change(value);
      return value;
    };
    await assert.rejects(recoverSeatbeltCI(options), /missing job total|population changed/);
  }
});

test('an overlapping page cannot replace undiscovered jobs', async () => {
  const { evidence, options } = harness();
  evidence.runs[0].jobs.jobs[2] = structuredClone(evidence.runs[0].jobs.jobs[0]);
  await assert.rejects(recoverSeatbeltCI(options), /duplicate job identity/);
});

test('a failed job on a later page prevents recovery even when workflows are green', async () => {
  const { evidence, options } = harness();
  evidence.runs[1].jobs.jobs.at(-1).conclusion = 'failure';
  await assert.rejects(recoverSeatbeltCI(options), /job failed or unfinished/);
});

test('jobs must bind to the exact run, attempt and head on every page', async () => {
  for (const [key, value] of [['run_id', 1], ['run_attempt', 2], ['head_sha', 'a'.repeat(40)]]) {
    const { evidence, options } = harness();
    evidence.runs[0].jobs.jobs[2][key] = value;
    await assert.rejects(recoverSeatbeltCI(options), /job (run|attempt|head) mismatch/);
  }
});

test('wrong-head, failed and unfinished runs are rejected before reading logs', async () => {
  for (const change of [run => { run.head_sha = 'a'.repeat(40); },
    run => { run.conclusion = 'failure'; }, run => { run.status = 'in_progress'; }]) {
    const { evidence, options } = harness();
    change(evidence.runs[0].run);
    options.fetchLog = async () => assert.fail('must not read unfinished or unrelated run logs');
    await assert.rejects(recoverSeatbeltCI(options), /workflow head|failed or unfinished/);
  }
});

test('a rerun starting during log recovery invalidates the captured attempt', async () => {
  const { options } = harness();
  const original = options.fetchJSON;
  const reads = new Map();
  options.fetchJSON = async endpoint => {
    const value = await original(endpoint);
    if (!endpoint.includes('/jobs?')) {
      reads.set(endpoint, (reads.get(endpoint) || 0) + 1);
      if (reads.get(endpoint) === 2) value.run_attempt += 1;
    }
    return value;
  };
  await assert.rejects(recoverSeatbeltCI(options), /workflow changed during recovery/);
});

test('wrong checkout and missing measured logs cannot be recovered as valid evidence', async () => {
  for (const log of ['', 'HEAD is now at abc Merge ' + 'a'.repeat(40) + ' into def']) {
    const { options } = harness();
    options.fetchLog = async () => log;
    await assert.rejects(recoverSeatbeltCI(options), /checkout head mismatch/);
  }
});

test('receipt selection omits unrelated runner log lines', async () => {
  const { options } = harness();
  const original = options.fetchLog;
  options.fetchLog = async id => 'unrelated setup output\n' + await original(id);
  const recovered = await recoverSeatbeltCI(options);
  assert.ok(recovered.receipts.every(receipt => receipt.selected_authentic_lines
    .every(line => !line.includes('unrelated setup'))));
});

test('unknown workflow sources and duplicate caller IDs are rejected', async () => {
  const { evidence, options } = harness();
  evidence.runs[0].run.path = '.github/workflows/unrelated.yml';
  await assert.rejects(recoverSeatbeltCI(options), /wrong workflow source/);
  options.runIDs[1] = options.runIDs[0];
  await assert.rejects(recoverSeatbeltCI(options), /duplicate run identity/);
});
