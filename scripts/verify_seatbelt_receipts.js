'use strict';

// Check retained native evidence against this candidate without launching an agent or
// rerunning sandbox-exec. Integrity checks are not a new native acceptance receipt.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const receiptPath = 'docs/verification/issue-532-seatbelt-regex/native-recovery-20261011';
const sourcePath = 'src/adapters.py';
const testPath = 'tests/test_seatbelt_regex_quoting.py';
const expectedFailures = {
  test_backslash: 'AssertionError: write allowed:',
  test_combined: 'sandbox-exec: Error reading string',
  test_quote: 'sandbox-exec: Error reading string',
  test_regex_metacharacters: 'sandbox-exec: unexpected ^ operator',
};

function digest(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function run(command, args, input) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { timeout: 30000 });
    const output = [];
    const errors = [];
    child.stdout.on('data', (chunk) => output.push(chunk));
    child.stderr.on('data', (chunk) => errors.push(chunk));
    child.on('error', reject);
    child.on('close', (code) => {
      if (code !== 0) reject(new Error(`${command} failed: ${Buffer.concat(errors)}`));
      else resolve(Buffer.concat(output));
    });
    child.stdin.on('error', reject);
    child.stdin.end(input);
  });
}

async function cases(xml) {
  // Use the standard XML parser rather than interpreting XML with a regex.
  const output = await run(process.env.PYTHON || 'python3', ['-I', '-c', `
import json, sys, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.buffer.read())
print(json.dumps([
    {"name": c.attrib["name"], "classname": c.attrib.get("classname"),
     "outcomes": [e.tag for e in c],
     "failure": "".join(c.find("failure").itertext()) if c.find("failure") is not None else ""}
    for c in root.iter("testcase")
]))
`], xml);
  return JSON.parse(output.toString('utf8'));
}

async function verifyProof(root = path.resolve(__dirname, '..')) {
  const receiptDir = path.join(root, receiptPath);
  const hashes = JSON.parse(fs.readFileSync(path.join(receiptDir, 'sha256.json'), 'utf8'));
  const archive = path.join(receiptDir, 'proof.tar.gz');
  assert.equal(digest(fs.readFileSync(archive)), hashes['proof.tar.gz'], 'archive digest mismatch');
  const listing = await run('tar', ['-tzf', archive]);
  const names = listing.toString('utf8').trim().split('\n');
  assert.equal(new Set(names).size, names.length, 'duplicate archive member');
  const members = new Map();
  for (const name of names) {
    assert.match(name, /^533-[a-z-]+\.(?:log|xml|json|py|md)$/, 'unexpected archive member');
    const bytes = await run('tar', ['-xOzf', archive, '--', name]);
    assert.equal(digest(bytes), hashes[name], `member digest mismatch: ${name}`);
    members.set(name, bytes);
  }
  const member = (name) => {
    assert.ok(members.has(name), `missing receipt: ${name}`);
    return members.get(name);
  };
  for (const file of [sourcePath, testPath]) {
    assert.equal(digest(fs.readFileSync(path.join(root, file))), hashes[file],
      `native evidence does not cover current ${file}`);
  }
  const results = JSON.parse(member('533-proof-results.json').toString('utf8'));
  assert.equal(results.original_restored_equal, true, 'byte-identical restoration not recorded');
  assert.equal(results.green.sha256, hashes[sourcePath], 'GREEN source digest mismatch');
  const source = fs.readFileSync(path.join(root, sourcePath), 'utf8');
  const anchor = '(regex {_seatbelt_quote(pattern)})';
  assert.equal(source.split(anchor).length - 1, 1, 'production reversal anchor must occur once');
  assert.equal(digest(source.replace(anchor, '(regex #{_seatbelt_quote(pattern)})')),
    results.red.sha256, 'RED did not use the recorded production reversal');
  assert.equal(results.red.exit, 1);
  assert.equal(results.red.cases, 4);
  assert.equal(results.red.failure, 4);
  assert.equal(results.green.exit, 0);
  assert.equal(results.green.cases, 4);
  assert.equal(results.green.pass, 4);
  assert.equal(results.green.native_probes, 5);
  for (const phase of ['red', 'green']) {
    const recorded = await cases(member(`533-proof-${phase}.xml`));
    assert.deepEqual(recorded.map((c) => c.name).sort(), Object.keys(expectedFailures).sort(),
      `${phase}: wrong test cases`);
    for (const c of recorded) {
      assert.equal(c.classname, 'tests.test_seatbelt_regex_quoting.SeatbeltRegexQuotingTests');
      assert.deepEqual(c.outcomes, phase === 'red' ? ['failure'] : [],
        `${phase}: skipped/error/nonpassing case ${c.name}`);
      if (phase === 'red') {
        assert.ok(c.failure.includes(expectedFailures[c.name]),
          `RED lacks the native failure for ${c.name}`);
      }
    }
  }
  const greenLog = member('533-proof-green.log').toString('utf8');
  assert.equal(greenLog.split('native Seatbelt probe: OK').length - 1, 5);
  assert.match(greenLog, /\b4 passed\b/);
  assert.doesNotMatch(greenLog, /UNKNOWN|\b\d+ (?:failed|skipped|errors?)\b/);
  return { source_sha256: results.green.sha256, red_failures: 4, green_passes: 4, native_probes: 5 };
}

// Repo-specific completion evidence for issue 532. This checks a retained REST snapshot,
// not live merge readiness, review threads, or the seven-minute review floor.
function verifyHostedCI(evidence, expectedHead) {
  assert.match(expectedHead, /^[a-f0-9]{40}$/, 'expected head must be an exact SHA');
  assert.equal(evidence.schema, 'seatbelt-hosted-ci/v1');
  assert.equal(evidence.head_sha, expectedHead, 'hosted evidence covers a different head');
  const required = {
    CI: {
      path: '.github/workflows/ci.yml',
      jobs: {
        'verify.py (real pytest counts + selftests + capability gates)': 'Verify',
        'combined coverage (pytest + selftests, the honest number)': 'Measure combined coverage',
        'verify.py in the flat exec-mirror shape (the tree a sync ships)':
          "Build the flat copy with the sync's copy contract and verify it there",
      },
    },
    Gate: {
      path: '.github/workflows/pr-00-gate.yml',
      jobs: {
        'python ci / python 3.12': 'Pytest (unit tests with coverage)',
        'python ci / python 3.13': 'Pytest (unit tests with coverage)',
        'python ci / lint-format': 'Black (format check)',
        'python ci / lint-ruff': 'Ruff (lint)',
        'python ci / typecheck-mypy': 'Mypy (type check)',
        summary: 'Summarize results',
      },
    },
  };
  const executed = new Map();
  assert.equal(evidence.runs.length, Object.keys(required).length, 'wrong workflow population');
  for (const [name, requirement] of Object.entries(required)) {
    const matches = evidence.runs.filter(item => item.run.name === name);
    assert.equal(matches.length, 1, `missing or duplicate workflow: ${name}`);
    const { run: workflow, jobs: population } = matches[0];
    assert.equal(workflow.path, requirement.path, 'wrong workflow source');
    assert.equal(workflow.event, 'pull_request', 'wrong workflow event');
    assert.equal(workflow.head_sha, expectedHead, 'workflow head mismatch');
    assert.equal(workflow.status, 'completed', 'workflow unfinished');
    assert.equal(workflow.conclusion, 'success', 'workflow failed');
    assert.ok(Number.isSafeInteger(workflow.id) && workflow.id > 0, 'missing run identity');
    assert.ok(Number.isSafeInteger(workflow.run_attempt) && workflow.run_attempt > 0,
      'missing run attempt');
    assert.equal(population.total_count, population.jobs.length, 'incomplete job discovery');
    assert.equal(new Set(population.jobs.map(job => job.id)).size, population.jobs.length,
      'duplicate job identity');
    for (const job of population.jobs) {
      assert.equal(job.run_id, workflow.id, 'job run mismatch');
      assert.equal(job.run_attempt, workflow.run_attempt, 'job attempt mismatch');
      assert.equal(job.head_sha, expectedHead, 'job head mismatch');
      assert.ok(job.status === 'completed' && ['success', 'skipped'].includes(job.conclusion),
        `failed or unfinished job: ${job.name}`);
    }
    for (const [jobName, stepName] of Object.entries(requirement.jobs)) {
      const jobs = population.jobs.filter(job => job.name === jobName);
      assert.equal(jobs.length, 1, `missing or duplicate product job: ${jobName}`);
      const job = jobs[0];
      assert.equal(job.conclusion, 'success', `product job did not execute: ${jobName}`);
      const steps = (job.steps || []).filter(step => step.name === stepName);
      assert.equal(steps.length, 1, `missing product step: ${stepName}`);
      assert.ok(steps[0].status === 'completed' && steps[0].conclusion === 'success',
        `product step did not execute: ${stepName}`);
      executed.set(jobName, job.id);
    }
  }
  const measurements = [];
  for (const [name, jobID] of executed) {
    if (!name.startsWith('verify.py')) continue;
    const receipts = evidence.receipts.filter(receipt => receipt.job_id === jobID);
    assert.equal(receipts.length, 1, `missing or duplicate measurement receipt: ${name}`);
    const text = receipts[0].selected_authentic_lines.join('\n');
    assert.ok(text.includes(`Merge ${expectedHead} into `), 'measurement checkout head mismatch');
    const count = /pytest:\s+(\d+) passed, 0 failed, (\d+)\/(\d+) max[^\n]*skipped \((\d+) collected; floor (\d+)\)/.exec(text);
    assert.ok(count, `missing test measurement: ${name}`);
    const [passed, skipped, skippedMax, collected, floor] = count.slice(1).map(Number);
    assert.ok(passed > 0 && passed + skipped === collected && collected >= floor
      && skipped <= skippedMax,
      `invalid test measurement: ${name}`);
    assert.ok(text.includes(`VERIFIED — ${passed} tests actually executed and passed,`),
      `missing verifier verdict measurement: ${name}`);
    measurements.push({ job_id: jobID, passed, skipped, collected });
  }
  return { head_sha: expectedHead, product_jobs: executed.size, measurements };
}

module.exports = { verifyProof, verifyHostedCI };
if (require.main === module) {
  verifyProof().then((result) => {
    const args = process.argv.slice(2);
    assert.ok(args.length === 0 || args.length === 2,
      'usage: node scripts/verify_seatbelt_receipts.js [hosted-ci.json expected-head-sha]');
    const hosted = args.length ? verifyHostedCI(JSON.parse(fs.readFileSync(args[0], 'utf8')),
      args[1]) : undefined;
    console.log(JSON.stringify({ retained_native_evidence: 'VERIFIED', ...result,
      ...(hosted ? { retained_hosted_ci: hosted } : {}) }));
  }).catch((error) => {
    console.error(`retained evidence: NOT VERIFIED: ${error.message}`);
    process.exitCode = 1;
  });
}
