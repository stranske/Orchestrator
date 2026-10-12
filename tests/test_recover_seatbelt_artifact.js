'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { recoverSeatbeltArtifact } = require('../scripts/recover_seatbelt_ci');

const dir = path.join(__dirname, '../docs/verification/issue-532-seatbelt-regex');
const discovered = JSON.parse(fs.readFileSync(path.join(dir,
  'original-artifact-discovery.json'), 'utf8'));
const archive = fs.readFileSync(path.join(dir,
  'original-ci-recovery-20261011/gate-coverage-3.12-1.zip'));
const artifactID = 11716675610;

function harness() {
  const artifacts = structuredClone(discovered.artifacts);
  // Only the artifact metadata/bytes are authentic here. REST totals, page boundaries
  // and run identity are simulated; this regression is not a recovered REST receipt.
  const run = { id: discovered.run_id, name: 'Gate', path: '.github/workflows/pr-00-gate.yml',
    event: 'pull_request', head_sha: discovered.head_sha, run_attempt: 1,
    status: 'completed', conclusion: 'success' };
  const calls = [];
  const options = { expectedHead: discovered.head_sha, runID: run.id, artifactID, pageSize: 2,
    fetchJSON: async endpoint => {
      calls.push(endpoint);
      if (endpoint.endsWith(`/runs/${run.id}`)) return structuredClone(run);
      const match = /\/artifacts\?per_page=2&page=(\d+)$/.exec(endpoint);
      assert.ok(match, `unexpected request: ${endpoint}`);
      const start = (Number(match[1]) - 1) * 2;
      return { total_count: artifacts.length,
        artifacts: structuredClone(artifacts.slice(start, start + 2)) };
    },
    fetchArchive: async id => {
      calls.push(`download:${id}`);
      assert.equal(id, artifactID);
      return archive;
    },
  };
  return { options, calls, run, artifacts };
}

test('finds the original archive beyond page one and verifies its authentic bytes', async () => {
  const { options, calls } = harness();
  const result = await recoverSeatbeltArtifact(options);
  assert.equal(result.archive, archive);
  assert.equal(result.evidence.artifacts.total_count, 8);
  assert.equal(result.evidence.artifacts.artifacts[4].id, artifactID);
  assert.equal(result.evidence.recovered.sha256,
    'c64668c7ad38cac9cf207f7538b21f58cef204da1dca936af31123f53e9a7bbb');
  assert.ok(calls.some(endpoint => endpoint.endsWith('&page=4')));
  assert.match(result.evidence.attempt_binding, /^UNKNOWN:/);
  assert.match(result.evidence.hosted_acceptance, /^UNKNOWN:/);
});

test('bounded connector metadata without a total cannot prove complete discovery', async () => {
  const { options } = harness();
  const original = options.fetchJSON;
  options.fetchJSON = async endpoint => {
    const value = await original(endpoint);
    if (endpoint.includes('/artifacts?')) delete value.total_count;
    return value;
  };
  await assert.rejects(recoverSeatbeltArtifact(options), /missing artifact total/);
});

test('empty, truncated, overlapping and changing later pages are rejected', async () => {
  for (const change of [
    value => { value.artifacts = []; },
    value => { value.artifacts.pop(); },
    value => { value.artifacts[0] = structuredClone(discovered.artifacts[0]); },
    value => { value.total_count += 1; },
    value => { value.total_count = 0; },
  ]) {
    const { options } = harness();
    const original = options.fetchJSON;
    options.fetchJSON = async endpoint => {
      const value = await original(endpoint);
      if (endpoint.endsWith('&page=2')) change(value);
      return value;
    };
    options.fetchArchive = async () => assert.fail('must finish discovery before downloading');
    await assert.rejects(recoverSeatbeltArtifact(options),
      /truncated artifact page|duplicate artifact identity|artifact population changed/);
  }
});

test('every artifact must belong to the requested run and head, including unselected ones', async () => {
  for (const [key, value] of [['id', 1], ['head_sha', 'a'.repeat(40)]]) {
    const { options, artifacts } = harness();
    artifacts.at(-1).workflow_run[key] = value;
    await assert.rejects(recoverSeatbeltArtifact(options), /artifact (run|head) mismatch/);
  }
});

test('an empty run or an absent requested artifact cannot trigger a download', async () => {
  for (const empty of [false, true]) {
    const { options, artifacts } = harness();
    if (empty) artifacts.length = 0;
    else options.artifactID = 1;
    options.fetchArchive = async () => assert.fail('must not download an undiscovered artifact');
    await assert.rejects(recoverSeatbeltArtifact(options), /requested artifact not found/);
  }
});

test('expired artifacts and missing digests or sizes stay unavailable', async () => {
  for (const [key, value, error] of [
    ['expired', true, /expired or unavailable/],
    ['expired', undefined, /expired or unavailable/],
    ['digest', undefined, /missing artifact digest|string/],
    ['digest', 'sha256:invalid', /missing artifact digest/],
    ['size_in_bytes', 0, /missing artifact size/],
  ]) {
    const { options, artifacts } = harness();
    artifacts.find(item => item.id === artifactID)[key] = value;
    options.fetchArchive = async () => assert.fail('must not download unavailable artifacts');
    await assert.rejects(recoverSeatbeltArtifact(options), error);
  }
});

test('truncated bytes, changed bytes and decoded text cannot replace the original ZIP', async () => {
  const changed = Buffer.from(archive);
  changed[0] ^= 1;
  for (const [bytes, error] of [[archive.subarray(1), /size mismatch/],
    [changed, /digest mismatch/], [archive.toString('utf8'), /must be bytes/]]) {
    const { options } = harness();
    options.fetchArchive = async () => bytes;
    await assert.rejects(recoverSeatbeltArtifact(options), error);
  }
});

test('a rerun during artifact download invalidates recovery', async () => {
  const { options, run } = harness();
  options.fetchArchive = async () => { run.run_attempt += 1; return archive; };
  await assert.rejects(recoverSeatbeltArtifact(options), /workflow changed during recovery/);
});

test('wrong-head and unfinished runs fail before artifact discovery', async () => {
  for (const change of [r => { r.head_sha = 'a'.repeat(40); },
    r => { r.status = 'in_progress'; }, r => { r.conclusion = 'failure'; },
    r => { r.path = '.github/workflows/other.yml'; }]) {
    const { options, run, calls } = harness();
    change(run);
    await assert.rejects(recoverSeatbeltArtifact(options),
      /workflow head mismatch|failed or unfinished|wrong workflow source/);
    assert.equal(calls.length, 1);
  }
});

test('invalid identities and page sizes cannot query GitHub', async () => {
  for (const [key, value] of [['expectedHead', 'main'], ['runID', 0],
    ['artifactID', 1.5], ['pageSize', 0], ['pageSize', 101]]) {
    const { options, calls } = harness();
    options[key] = value;
    await assert.rejects(recoverSeatbeltArtifact(options));
    assert.deepEqual(calls, []);
  }
});
