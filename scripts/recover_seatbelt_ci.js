'use strict';

// Issue 532's repo-specific receipt recovery. Read existing runs only; never rerun jobs.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { verifyDiscoveredCI } = require('./verify_seatbelt_receipts');

const repo = 'stranske/Orchestrator';
const workflowPaths = {
  CI: '.github/workflows/ci.yml',
  Gate: '.github/workflows/pr-00-gate.yml',
};

function identity(run) {
  return [run.id, run.name, run.path, run.event, run.head_sha, run.run_attempt,
    run.status, run.conclusion];
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
      const log = await fetchLog(job.id);
      receipts.push({ job_id: job.id, url: job.html_url,
        selected_authentic_lines: log.split(/\r?\n/).filter(line =>
          /HEAD is now at .* Merge |\s+pytest:\s+|\s+selftests:\s+|\s+gates:\s+|VERIFIED — |== verify-before-sync: VERIFIED/.test(line)) });
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

module.exports = { recoverSeatbeltCI };

if (require.main === module) {
  const [expectedHead, ...ids] = process.argv.slice(2);
  const read = endpoint => execFileSync('gh', ['api', endpoint],
    { encoding: 'utf8', timeout: 30000, maxBuffer: 32 * 1024 * 1024 });
  recoverSeatbeltCI({ expectedHead, runIDs: ids.map(Number),
    fetchJSON: async endpoint => JSON.parse(read(endpoint)),
    fetchLog: async id => read(`repos/${repo}/actions/jobs/${id}/logs`),
  }).then(snapshot => console.log(JSON.stringify(snapshot, null, 2))).catch(error => {
    console.error(`CI recovery failed: ${error.message}`);
    process.exitCode = 1;
  });
}
