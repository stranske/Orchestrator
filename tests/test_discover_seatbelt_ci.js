'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { discoverSeatbeltCIRuns } = require('../scripts/recover_seatbelt_ci');

const retained = JSON.parse(fs.readFileSync(path.join(__dirname,
  '../docs/verification/issue-532-seatbelt-regex/c49b250-hosted-ci.json'), 'utf8'));

function harness() {
  const runs = retained.runs.map(entry => structuredClone(entry.run));
  // Synthetic same-head history deliberately puts non-green candidates on later pages.
  runs.push({ ...runs[1], id: 999001, conclusion: 'cancelled' },
    { ...runs[0], id: 999002, conclusion: 'failure' },
    { ...runs[0], id: 999003, status: 'in_progress', conclusion: null },
    { ...runs[0], id: 999004, name: 'Unrelated workflow', path: '.github/workflows/other.yml' });
  const calls = [];
  const options = { expectedHead: retained.head_sha, pageSize: 2,
    fetchJSON: async endpoint => {
      calls.push(endpoint);
      const match = /^repos\/stranske\/Orchestrator\/actions\/runs\?head_sha=([a-f0-9]{40})&event=pull_request&per_page=(\d+)&page=(\d+)$/.exec(endpoint);
      assert.ok(match, `unexpected request: ${endpoint}`);
      assert.equal(match[1], options.expectedHead);
      const start = (Number(match[3]) - 1) * Number(match[2]);
      return { total_count: runs.length,
        workflow_runs: structuredClone(runs.slice(start, start + Number(match[2]))) };
    },
  };
  return { runs, calls, options };
}

test('discovery rescans all exact-head pages and keeps non-green candidates', async () => {
  const { runs, calls, options } = harness();
  const result = await discoverSeatbeltCIRuns(options);
  assert.equal(result.schema, 'seatbelt-ci-run-discovery/v1');
  assert.equal(result.total_count, runs.length);
  assert.equal(result.runs.length, runs.length);
  assert.deepEqual(result.candidates, { CI: [999002, 999003, retained.runs[0].run.id],
    Gate: [999001, retained.runs[1].run.id] });
  assert.equal(result.runs.find(run => run.id === 999003).conclusion, null);
  assert.equal(result.runs.find(run => run.id === 999002).conclusion, 'failure');
  assert.deepEqual(calls.map(call => call.slice(-6)),
    ['page=1', 'page=2', 'page=3', 'page=1', 'page=2', 'page=3']);
  assert.ok(calls.every(call => !call.includes('status=success')));
  assert.match(result.hosted_acceptance, /^UNKNOWN:/);
});

test('zero runs is complete discovery with no acceptance or selected workflow', async () => {
  const { runs, options } = harness();
  runs.length = 0;
  const result = await discoverSeatbeltCIRuns(options);
  assert.equal(result.total_count, 0);
  assert.deepEqual(result.candidates, { CI: [], Gate: [] });
  assert.match(result.hosted_acceptance, /^UNKNOWN:/);
});

test('retained authentic run records survive simulated pagination unchanged', async () => {
  for (const [filename, expectedTotal, expectedCandidates] of [
    ['original-run-discovery-20261012.json', 13,
      { CI: [38176808788], Gate: [38176809124, 38178578389] }],
    ['f8fff11-run-discovery.json', 9,
      { CI: [38212775055], Gate: [38212775500, 38212780773] }],
  ]) {
    const snapshot = JSON.parse(fs.readFileSync(path.join(__dirname,
      '../docs/verification/issue-532-seatbelt-regex', filename), 'utf8'));
    const { recovery, ...expected } = snapshot;
    let calls = 0;
    const result = await discoverSeatbeltCIRuns({ expectedHead: snapshot.head_sha,
      pageSize: recovery.page_size,
      fetchJSON: async endpoint => {
        calls += 1;
        const match = /&page=(\d+)$/.exec(endpoint);
        assert.ok(endpoint.includes(`head_sha=${snapshot.head_sha}&event=pull_request&`));
        const start = (Number(match[1]) - 1) * recovery.page_size;
        return { total_count: snapshot.total_count,
          workflow_runs: structuredClone(snapshot.runs.slice(start, start + recovery.page_size)) };
      },
    });
    assert.deepEqual(result, expected);
    assert.equal(result.total_count, expectedTotal);
    assert.deepEqual(result.candidates, expectedCandidates);
    assert.equal(calls, recovery.read_count);
    assert.equal(recovery.collection_scans, 2);
  }
});

test('discovery never publishes unrelated runner or actor data', async () => {
  const { runs, options } = harness();
  runs[0].actor = { login: 'synthetic-user' };
  runs[0].unneeded_runner_data = 'synthetic-private-environment';
  const result = await discoverSeatbeltCIRuns(options);
  assert.ok(result.runs.every(run => !Object.hasOwn(run, 'actor')
    && !Object.hasOwn(run, 'unneeded_runner_data')));
});

test('empty and truncated later pages cannot establish a complete run population', async () => {
  for (const count of [0, 1]) {
    const { options } = harness();
    const original = options.fetchJSON;
    options.fetchJSON = async endpoint => {
      const value = await original(endpoint);
      if (endpoint.endsWith('page=2')) value.workflow_runs = value.workflow_runs.slice(0, count);
      return value;
    };
    await assert.rejects(discoverSeatbeltCIRuns(options), /truncated run page/);
  }
});

test('missing and changing declared totals cannot mask omitted runs', async () => {
  for (const change of [value => { delete value.total_count; },
    value => { value.total_count += 1; }]) {
    const { options } = harness();
    const original = options.fetchJSON;
    options.fetchJSON = async endpoint => {
      const value = await original(endpoint);
      if (endpoint.endsWith('page=2')) change(value);
      return value;
    };
    await assert.rejects(discoverSeatbeltCIRuns(options), /missing run total|population changed/);
  }
});

test('a run population at or above the filtered API search cap cannot claim completeness', async () => {
  for (const total_count of [1000, 1001]) {
    const { options } = harness();
    let calls = 0;
    options.fetchJSON = async () => {
      calls += 1;
      return { total_count, workflow_runs: [] };
    };
    await assert.rejects(discoverSeatbeltCIRuns(options), /GitHub search cap/);
    assert.equal(calls, 1);
  }
});

test('overlapping pages and populations exceeding the total are rejected', async () => {
  const duplicate = harness();
  duplicate.runs[2] = structuredClone(duplicate.runs[0]);
  await assert.rejects(discoverSeatbeltCIRuns(duplicate.options), /duplicate run identity/);
  const excess = harness();
  const original = excess.options.fetchJSON;
  excess.options.fetchJSON = async endpoint => ({ ...await original(endpoint), total_count: 1 });
  await assert.rejects(discoverSeatbeltCIRuns(excess.options), /population exceeds total/);
});

test('wrong heads, events, sources and missing identity metadata fail on later pages', async () => {
  for (const [key, value, error] of [
    ['id', undefined, /missing run identity/],
    ['head_sha', 'a'.repeat(40), /workflow head mismatch/],
    ['event', 'push', /wrong workflow event/],
    ['path', '.github/workflows/other.yml', /wrong workflow source/],
    ['run_attempt', undefined, /missing run attempt/],
    ['status', undefined, /missing workflow status/],
    ['conclusion', undefined, /missing workflow status/],
  ]) {
    const { runs, options } = harness();
    runs[2][key] = value;
    await assert.rejects(discoverSeatbeltCIRuns(options), error);
  }
});

test('new, removed or replaced runs during discovery invalidate the collection', async () => {
  for (const change of [
    runs => runs.push({ ...runs[0], id: 999005 }),
    runs => runs.pop(),
    runs => { runs[2].id = 999005; },
  ]) {
    const { runs, options } = harness();
    const original = options.fetchJSON;
    let scans = 0;
    options.fetchJSON = async endpoint => {
      if (endpoint.endsWith('page=1') && ++scans === 2) change(runs);
      return original(endpoint);
    };
    await assert.rejects(discoverSeatbeltCIRuns(options), /population changed/);
  }
});

test('reruns and status changes invalidate discovery even when total and IDs stay stable', async () => {
  for (const change of [
    run => { run.run_attempt += 1; },
    run => { run.conclusion = 'success'; },
    run => { run.status = 'in_progress'; run.conclusion = null; },
  ]) {
    const { runs, options } = harness();
    const original = options.fetchJSON;
    let scans = 0;
    options.fetchJSON = async endpoint => {
      if (endpoint.endsWith('page=1') && ++scans === 2) change(runs[2]);
      return original(endpoint);
    };
    await assert.rejects(discoverSeatbeltCIRuns(options), /population changed/);
  }
});

test('listing order alone does not change the discovered identities', async () => {
  const { runs, options } = harness();
  const original = options.fetchJSON;
  let scans = 0;
  options.fetchJSON = async endpoint => {
    if (endpoint.endsWith('page=1') && ++scans === 2) runs.reverse();
    return original(endpoint);
  };
  assert.equal((await discoverSeatbeltCIRuns(options)).total_count, runs.length);
});

test('invalid heads and page sizes are refused before any request', async () => {
  for (const changes of [{ expectedHead: 'main' }, { pageSize: 0 }, { pageSize: 101 }]) {
    await assert.rejects(discoverSeatbeltCIRuns({ ...harness().options, ...changes,
      fetchJSON: async () => assert.fail('invalid input must not fetch') }),
    /exact SHA|invalid page size/);
  }
});
