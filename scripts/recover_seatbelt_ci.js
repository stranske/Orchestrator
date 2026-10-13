'use strict';

// Issue 532's repo-specific receipt recovery. Read existing runs only; never rerun jobs.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const { execFileSync } = require('node:child_process');
const { verifyDiscoveredCI, selectMeasurementLines } = require('./verify_seatbelt_receipts');

const repo = 'stranske/Orchestrator';
const workflowPaths = {
  CI: '.github/workflows/ci.yml',
  Gate: '.github/workflows/pr-00-gate.yml',
};

function identity(run) {
  return [run.id, run.name, run.path, run.event, run.head_sha, run.run_attempt,
    run.status, run.conclusion];
}

// Discover every exact-head PR run before choosing IDs for receipt recovery. In particular,
// never filter for success: a later green Gate must not hide an earlier failed/cancelled run.
async function discoverSeatbeltCIRuns({ expectedHead, fetchJSON, pageSize = 100 }) {
  assert.match(expectedHead, /^[a-f0-9]{40}$/, 'expected head must be an exact SHA');
  assert.ok(Number.isInteger(pageSize) && pageSize > 0 && pageSize <= 100, 'invalid page size');
  const endpoint = `repos/${repo}/actions/runs?head_sha=${expectedHead}`
    + `&event=pull_request&per_page=${pageSize}`;
  async function scan() {
    const runs = [];
    const seen = new Set();
    let total;
    for (let page = 1; ; page += 1) {
      const population = await fetchJSON(`${endpoint}&page=${page}`);
      assert.ok(Number.isSafeInteger(population.total_count) && population.total_count >= 0,
        'missing run total');
      // Filtered Actions run searches are capped at 1,000 results. At the cap even a
      // stable declared total cannot establish that no additional runs were omitted.
      assert.ok(population.total_count < 1000, 'run discovery reaches the GitHub search cap');
      if (total === undefined) total = population.total_count;
      assert.equal(population.total_count, total, 'run population changed during discovery');
      assert.ok(Array.isArray(population.workflow_runs)
        && population.workflow_runs.length <= pageSize, 'invalid run page');
      for (const run of population.workflow_runs) {
        assert.ok(Number.isSafeInteger(run.id) && run.id > 0, 'missing run identity');
        assert.ok(!seen.has(run.id), 'duplicate run identity across pages');
        seen.add(run.id);
        assert.equal(run.head_sha, expectedHead, 'workflow head mismatch');
        assert.equal(run.event, 'pull_request', 'wrong workflow event');
        assert.ok(Number.isSafeInteger(run.run_attempt) && run.run_attempt > 0,
          'missing run attempt');
        assert.ok(typeof run.name === 'string' && run.name.trim()
          && typeof run.path === 'string' && run.path.trim(), 'missing workflow source');
        assert.ok(typeof run.status === 'string' && run.status.trim()
          && (run.conclusion === null || typeof run.conclusion === 'string'),
          'missing workflow status');
        if (Object.hasOwn(workflowPaths, run.name)) {
          assert.equal(run.path, workflowPaths[run.name], 'wrong workflow source');
        }
        runs.push(Object.fromEntries(['id', 'name', 'path', 'event', 'head_sha',
          'run_attempt', 'status', 'conclusion', 'html_url'].map(key => [key, run[key]])));
      }
      assert.ok(runs.length <= total, 'run population exceeds total');
      if (runs.length === total) break;
      assert.equal(population.workflow_runs.length, pageSize, 'truncated run page');
    }
    return { total_count: total, runs: runs.sort((a, b) => a.id - b.id) };
  }
  const population = await scan();
  // Rescan the collection, not just known IDs: a new run can appear while older pages are read.
  const rechecked = await scan();
  assert.deepEqual(rechecked.runs.map(identity), population.runs.map(identity),
    'run population changed during discovery');
  return { schema: 'seatbelt-ci-run-discovery/v1', repo, head_sha: expectedHead,
    ...population,
    candidates: Object.fromEntries(Object.keys(workflowPaths).map(name => [name,
      population.runs.filter(run => run.name === name).map(run => run.id)])),
    hosted_acceptance: 'UNKNOWN: run discovery alone does not prove jobs or producer measurements' };
}

async function recoverSeatbeltCI({ expectedHead, runIDs, fetchJSON, fetchLog, pageSize = 100 }) {
  assert.match(expectedHead, /^[a-f0-9]{40}$/, 'expected head must be an exact SHA');
  assert.equal(runIDs.length, 2, 'supply the existing CI and Gate run IDs');
  assert.equal(new Set(runIDs).size, 2, 'duplicate run identity');
  assert.ok(runIDs.every(id => Number.isSafeInteger(id) && id > 0), 'invalid run identity');
  assert.ok(Number.isInteger(pageSize) && pageSize > 0 && pageSize <= 100, 'invalid page size');
  const runs = [];
  const receipts = [];
  const seenJobs = new Set();
  for (const id of runIDs) {
    const endpoint = `repos/${repo}/actions/runs/${id}`;
    const run = await fetchJSON(endpoint);
    assert.equal(run.id, id, 'run identity mismatch');
    assert.ok(Object.hasOwn(workflowPaths, run.name), 'unexpected workflow');
    assert.equal(run.path, workflowPaths[run.name], 'wrong workflow source');
    assert.equal(run.event, 'pull_request', 'wrong workflow event');
    assert.equal(run.head_sha, expectedHead, 'workflow head mismatch');
    assert.ok(Number.isSafeInteger(run.run_attempt) && run.run_attempt > 0,
      'missing run attempt');
    assert.ok(run.status === 'completed' && run.conclusion === 'success',
      'workflow failed or unfinished');
    const jobs = [];
    let total;
    for (let page = 1; ; page += 1) {
      const population = await fetchJSON(`${endpoint}/attempts/${run.run_attempt}/jobs`
        + `?per_page=${pageSize}&page=${page}`);
      assert.ok(Number.isSafeInteger(population.total_count) && population.total_count > 0,
        'missing job total');
      if (total === undefined) total = population.total_count;
      assert.equal(population.total_count, total, 'job population changed during recovery');
      assert.ok(Array.isArray(population.jobs) && population.jobs.length > 0
        && population.jobs.length <= pageSize, 'incomplete job page');
      for (const job of population.jobs) {
        assert.ok(Number.isSafeInteger(job.id) && job.id > 0, 'missing job identity');
        assert.ok(!seenJobs.has(job.id), 'duplicate job identity across pages or runs');
        seenJobs.add(job.id);
        assert.equal(job.run_id, id, 'job run mismatch');
        assert.equal(job.run_attempt, run.run_attempt, 'job attempt mismatch');
        assert.equal(job.head_sha, expectedHead, 'job head mismatch');
        assert.ok(job.status === 'completed' && ['success', 'skipped'].includes(job.conclusion),
          'job failed or unfinished');
        jobs.push(job);
      }
      assert.ok(jobs.length <= total, 'job population exceeds total');
      if (jobs.length === total) break;
      assert.equal(population.jobs.length, pageSize, 'truncated job page');
    }
    // Retain only the fields used by the checker; do not publish runner environment logs.
    runs.push({ run: Object.fromEntries(['id', 'name', 'path', 'event', 'head_sha',
      'run_attempt', 'status', 'conclusion', 'html_url'].map(key => [key, run[key]])),
    jobs: { total_count: total, jobs: jobs.map(job => Object.fromEntries(
      ['id', 'name', 'run_id', 'run_attempt', 'head_sha', 'status', 'conclusion',
        'html_url', 'steps'].map(key => [key, job[key]]))) } });
    for (const job of jobs.filter(job => run.name === 'CI' && job.name.startsWith('verify.py'))) {
      // fetchLog must return gh's job/step-prefixed log, never the raw whole-job API log.
      const log = await fetchLog(job.id, run.run_attempt);
      const stepName = job.name.includes('flat exec-mirror')
        ? "Build the flat copy with the sync's copy contract and verify it there" : 'Verify';
      receipts.push({ job_id: job.id, url: job.html_url,
        selected_authentic_lines: selectMeasurementLines(log, job, stepName) });
    }
    // A concurrent rerun must not attach latest-attempt logs to older job metadata.
    assert.deepEqual(identity(await fetchJSON(endpoint)), identity(run),
      'workflow changed during recovery');
  }
  const snapshot = { schema: 'seatbelt-hosted-ci/v1', repo, head_sha: expectedHead,
    runs, receipts };
  // Check measured CI products even when Gate legitimately skipped its Python matrix.
  // Complete metadata recovery alone does not confer hosted acceptance.
  verifyDiscoveredCI({ schema: 'seatbelt-ci-discovery/v1', repo, head_sha: expectedHead,
    discovery_limit: 'Internal measurement validation only; full metadata retained separately',
    runs: runs.map(({ run, jobs }) => ({ name: run.name, run_id: run.id,
      jobs: jobs.jobs.map(job => ({ ...job, product_steps: job.steps || [] })) })),
    receipts }, expectedHead);
  return snapshot;
}

// Artifacts are listed per run, not per attempt. Retain that limitation even after
// complete pagination and a verified download; a digest is not a product verdict.
async function recoverSeatbeltArtifact({ expectedHead, runID, artifactID,
  fetchJSON, fetchArchive, pageSize = 100 }) {
  assert.match(expectedHead, /^[a-f0-9]{40}$/, 'expected head must be an exact SHA');
  assert.ok([runID, artifactID].every(id => Number.isSafeInteger(id) && id > 0),
    'invalid run or artifact identity');
  assert.ok(Number.isInteger(pageSize) && pageSize > 0 && pageSize <= 100, 'invalid page size');
  const endpoint = `repos/${repo}/actions/runs/${runID}`;
  const run = await fetchJSON(endpoint);
  assert.equal(run.id, runID, 'run identity mismatch');
  assert.ok(Object.hasOwn(workflowPaths, run.name), 'unexpected workflow');
  assert.equal(run.path, workflowPaths[run.name], 'wrong workflow source');
  assert.equal(run.event, 'pull_request', 'wrong workflow event');
  assert.equal(run.head_sha, expectedHead, 'workflow head mismatch');
  assert.ok(Number.isSafeInteger(run.run_attempt) && run.run_attempt > 0, 'missing run attempt');
  assert.ok(run.status === 'completed' && run.conclusion === 'success',
    'workflow failed or unfinished');
  const artifacts = [];
  const seen = new Set();
  let total;
  for (let page = 1; ; page += 1) {
    const population = await fetchJSON(`${endpoint}/artifacts?per_page=${pageSize}&page=${page}`);
    assert.ok(Number.isSafeInteger(population.total_count) && population.total_count >= 0,
      'missing artifact total');
    if (total === undefined) total = population.total_count;
    assert.equal(population.total_count, total, 'artifact population changed during recovery');
    assert.ok(Array.isArray(population.artifacts) && population.artifacts.length <= pageSize,
      'invalid artifact page');
    for (const artifact of population.artifacts) {
      assert.ok(Number.isSafeInteger(artifact.id) && artifact.id > 0, 'missing artifact identity');
      assert.ok(!seen.has(artifact.id), 'duplicate artifact identity across pages');
      seen.add(artifact.id);
      assert.equal(artifact.workflow_run?.id, runID, 'artifact run mismatch');
      assert.equal(artifact.workflow_run?.head_sha, expectedHead, 'artifact head mismatch');
      artifacts.push(Object.fromEntries(['id', 'name', 'size_in_bytes', 'expired',
        'created_at', 'updated_at', 'expires_at', 'digest', 'workflow_run']
        .map(key => [key, artifact[key]])));
    }
    assert.ok(artifacts.length <= total, 'artifact population exceeds total');
    if (artifacts.length === total) break;
    assert.equal(population.artifacts.length, pageSize, 'truncated artifact page');
  }
  const artifact = artifacts.find(item => item.id === artifactID);
  assert.ok(artifact, 'requested artifact not found in complete population');
  assert.equal(artifact.expired, false, 'requested artifact expired or unavailable');
  assert.match(artifact.digest, /^sha256:[a-f0-9]{64}$/, 'missing artifact digest');
  assert.ok(Number.isSafeInteger(artifact.size_in_bytes) && artifact.size_in_bytes > 0,
    'missing artifact size');
  // Download by validated identity, never by a URL or filename supplied by the artifact.
  const archive = await fetchArchive(artifact.id);
  assert.ok(Buffer.isBuffer(archive), 'artifact download must be bytes');
  assert.equal(archive.length, artifact.size_in_bytes, 'artifact size mismatch');
  const sha256 = crypto.createHash('sha256').update(archive).digest('hex');
  assert.equal(`sha256:${sha256}`, artifact.digest, 'artifact digest mismatch');
  assert.deepEqual(identity(await fetchJSON(endpoint)), identity(run),
    'workflow changed during recovery');
  return { archive, evidence: { schema: 'seatbelt-ci-artifact/v1', repo,
    head_sha: expectedHead, run: Object.fromEntries(['id', 'name', 'path', 'event',
      'head_sha', 'run_attempt', 'status', 'conclusion', 'html_url']
      .map(key => [key, run[key]])), artifacts: { total_count: total, artifacts },
    recovered: { artifact_id: artifactID, sha256, size_in_bytes: archive.length },
    attempt_binding: 'UNKNOWN: run artifact listings do not identify the producing attempt',
    hosted_acceptance: 'UNKNOWN: artifact recovery alone does not prove product execution' } };
}

module.exports = { discoverSeatbeltCIRuns, recoverSeatbeltCI, recoverSeatbeltArtifact };

if (require.main === module) {
  const args = process.argv.slice(2);
  const artifactMode = args[0] === '--artifact';
  const discoveryMode = args[0] === '--discover';
  if (artifactMode || discoveryMode) args.shift();
  const [expectedHead, ...ids] = args;
  const read = endpoint => execFileSync('gh', ['api', endpoint],
    { encoding: 'utf8', timeout: 30000, maxBuffer: 32 * 1024 * 1024 });
  const recovery = discoveryMode ? (async () => {
    assert.equal(ids.length, 0, 'discovery accepts only an exact head SHA');
    return discoverSeatbeltCIRuns({ expectedHead,
      fetchJSON: async endpoint => JSON.parse(read(endpoint)) });
  })() : artifactMode ? (async () => {
    assert.equal(ids.length, 3, 'supply run ID, artifact ID and a new archive destination');
    const { evidence, archive } = await recoverSeatbeltArtifact({ expectedHead,
      runID: Number(ids[0]), artifactID: Number(ids[1]),
      fetchJSON: async endpoint => JSON.parse(read(endpoint)),
      fetchArchive: async id => execFileSync('gh', ['api',
        `repos/${repo}/actions/artifacts/${id}/zip`],
      { timeout: 30000, maxBuffer: 32 * 1024 * 1024 }),
    });
    fs.writeFileSync(ids[2], archive, { flag: 'wx' });
    return evidence;
  })() : recoverSeatbeltCI({ expectedHead, runIDs: ids.map(Number),
    fetchJSON: async endpoint => JSON.parse(read(endpoint)),
    fetchLog: async (id, attempt) => execFileSync('gh', ['run', 'view', '--repo', repo,
      '--job', String(id), '--attempt', String(attempt), '--log'],
    { encoding: 'utf8', timeout: 30000, maxBuffer: 32 * 1024 * 1024 }),
  });
  recovery.then(snapshot => console.log(JSON.stringify(snapshot, null, 2))).catch(error => {
    console.error(`CI recovery failed: ${error.message}`);
    process.exitCode = 1;
  });
}
