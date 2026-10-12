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

// Verify the original focused cases directly against the recovered full-run artifact.
// This establishes retained results, not a separate historical focused command or live CI.
async function verifyOriginalCI(root = path.resolve(__dirname, '..')) {
  const dir = path.join(root,
    'docs/verification/issue-532-seatbelt-regex/original-ci-recovery-20261011');
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
  const inherited = JSON.parse(fs.readFileSync(path.join(dir, '../inherited-ci.json'), 'utf8'));
  assert.equal(manifest.schema, 'original-ci-recovery/v1');
  assert.equal(manifest.repo, 'stranske/Orchestrator');
  assert.equal(manifest.repo, inherited.repository, 'original repository mismatch');
  assert.equal(manifest.source_pr, inherited.inherited_pr, 'original PR mismatch');
  assert.equal(manifest.original_head, inherited.head_sha, 'original head mismatch');
  assert.equal(manifest.original_merge, inherited.merge_sha, 'original merge mismatch');
  const archiveName = 'gate-coverage-3.12-1.zip';
  const extractionName = 'original-boundary-cases.xml';
  assert.equal(manifest.artifact_name, 'gate-coverage-3.12-1');
  assert.equal(manifest.artifact_id, 11716675610, 'wrong original artifact');
  assert.equal(manifest.run_id, 38176809124, 'wrong original run');
  const job = inherited.jobs.find(item => item.run_id === manifest.run_id);
  assert.ok(job, 'missing original job binding');
  const log = job.excerpts.join('\n');
  assert.ok(log.includes(`Merge ${manifest.original_head} into `), 'original checkout mismatch');
  const measured = /\b(\d+) passed, (\d+) skipped\b/.exec(log);
  assert.ok(measured, 'missing original measured counts');
  const expectedFiles = [archiveName, extractionName, 'README.md',
    'independent-hash-check.json', 'independent-native.log', 'independent-native.xml'];
  assert.deepEqual(Object.keys(manifest.files).sort(), expectedFiles.sort(),
    'incomplete original receipt manifest');
  for (const file of expectedFiles) {
    assert.equal(digest(fs.readFileSync(path.join(dir, file))), manifest.files[file],
      `original receipt digest mismatch: ${file}`);
  }
  assert.equal(manifest.artifact_digest, manifest.files[archiveName], 'artifact digest mismatch');
  // Standard-library ZIP and XML parsers; do not extract files or execute artifact content.
  const output = await run(process.env.PYTHON || 'python3', ['-I', '-c', `
import hashlib, json, sys, zipfile, xml.etree.ElementTree as ET
with zipfile.ZipFile(sys.argv[1]) as archive:
    names = archive.namelist()
    assert len(names) == len(set(names)), "duplicate archive member"
    assert set(names) == {"index.ndjson", "runtimes/3.12/coverage.json",
        "runtimes/3.12/coverage.xml", "runtimes/3.12/pytest-junit.xml",
        "runtimes/3.12/summary.json"}, "unexpected original artifact members"
    assert archive.testzip() is None, "invalid archive CRC"
    junit = archive.read("runtimes/3.12/pytest-junit.xml")
    summary = json.loads(archive.read("runtimes/3.12/summary.json"))
root = ET.fromstring(junit)
records = list(root.iter("testcase"))
def record(element):
    return {"tag": element.tag, "attributes": element.attrib,
        "text": (element.text or "").strip(), "children": [record(c) for c in element]}
focused = [c for c in records if c.get("classname") == "tests.test_offload_write_boundary"]
extracted = ET.parse(sys.argv[2]).getroot()
print(json.dumps({
    "junit_sha256": hashlib.sha256(junit).hexdigest(),
    "summary": summary,
    "tests": len(records),
    "unique_tests": len({(c.get("classname"), c.get("name")) for c in records}),
    "failed": sum(c.find("failure") is not None for c in records),
    "errors": sum(c.find("error") is not None for c in records),
    "skipped": sum(c.find("skipped") is not None for c in records),
    "reported_tests": [int(s.get("tests")) for s in root.iter("testsuite")],
    "focused": [record(c) for c in focused],
    "extracted": [record(c) for c in extracted.iter("testcase")]
}))
`, path.join(dir, archiveName), path.join(dir, extractionName)]);
  const result = JSON.parse(output.toString('utf8'));
  assert.equal(result.junit_sha256, manifest.original_junit_sha256, 'original JUnit digest mismatch');
  assert.equal(result.summary.python_version, '3.12');
  assert.equal(result.summary.checks.tests.outcome, 'success', 'original product tests failed');
  assert.equal(result.failed, 0, 'original failed cases');
  assert.equal(result.errors, 0, 'original error cases');
  assert.equal(result.tests, result.unique_tests, 'duplicate original cases');
  assert.equal(result.skipped, Number(measured[2]), 'original skip count mismatch');
  assert.equal(result.tests - result.skipped, Number(measured[1]), 'original pass count mismatch');
  const expectedNames = [
    'test_codex_gets_exactly_its_roots_and_no_seatbelt',
    ...['cursor', 'gemini', 'vibe'].map(agent =>
      `test_an_agent_without_a_sandbox_runs_under_the_profile[${agent}]`),
    'test_no_boundary_changes_nothing',
    ...['empty_protect', 'home_granted', 'codex_bypassed', 'no_sandbox_exec',
      'nested_seatbelt'].map(c => `test_a_boundary_that_cannot_hold_refuses_the_run[${c}]`),
    'test_roots_resolve_and_lead_with_the_cwd',
    'test_the_profile_quotes_a_path_it_cannot_otherwise_name',
  ];
  assert.equal(manifest.focused_cases, expectedNames.length);
  assert.deepEqual(result.focused.map(c => c.attributes.name).sort(), expectedNames.sort(),
    'missing or duplicate original boundary cases');
  for (const c of result.focused) {
    assert.deepEqual(c.children, [], `original focused case did not pass: ${c.attributes.name}`);
  }
  assert.deepEqual(result.extracted, result.focused, 'extracted cases differ from original JUnit');
  return { original_head: manifest.original_head, run_id: manifest.run_id,
    artifact_id: manifest.artifact_id, focused_passes: result.focused.length,
    passed: result.tests - result.skipped, skipped: result.skipped, collected: result.tests,
    // Retain the authentic summary discrepancy rather than treating it as a measured count.
    junit_reported_tests: result.reported_tests,
    focused_command_transcript: 'UNKNOWN: recovered cases are from the original full run' };
}

function verifyMeasurement(receipt, expectedHead, name) {
  const text = receipt.selected_authentic_lines.join('\n');
  assert.ok(text.includes(`Merge ${expectedHead} into `), 'measurement checkout head mismatch');
  const count = /pytest:\s+(\d+) passed, 0 failed, (\d+)\/(\d+) max[^\n]*skipped \((\d+) collected; floor (\d+)\)/.exec(text);
  assert.ok(count, `missing test measurement: ${name}`);
  const [passed, skipped, skippedMax, collected, floor] = count.slice(1).map(Number);
  assert.ok(passed > 0 && passed + skipped === collected && collected >= floor
    && skipped <= skippedMax, `invalid test measurement: ${name}`);
  const selftests = /selftests:\s+(\d+) of (\d+) modules ran, (\d+)\/(\d+) max[^\n]*skipped/.exec(text);
  assert.ok(selftests, `missing selftest measurement: ${name}`);
  const [ran, modules, absent, absentMax] = selftests.slice(1).map(Number);
  assert.ok(ran > 0 && ran + absent === modules && absent <= absentMax,
    `invalid selftest measurement: ${name}`);
  const verdict = /VERIFIED — (\d+) tests actually executed and passed, (\d+) selftests spoke, (\d+) of (\d+) gates green/.exec(text);
  assert.ok(verdict, `missing verifier verdict measurement: ${name}`);
  assert.equal(Number(verdict[1]), passed, 'verifier verdict test count mismatch');
  assert.equal(Number(verdict[2]), ran, 'verifier verdict selftest count mismatch');
  const [green, gates] = verdict.slice(3).map(Number);
  assert.ok(green > 0 && green <= gates, `invalid gate measurement: ${name}`);
  return { job_id: receipt.job_id, passed, skipped, collected, selftests: ran,
    selftests_skipped: absent, gates_green: green, gates };
}

// Recover usable measurements from bounded connector discovery without promoting a first page
// to complete hosted acceptance. Missing attempt/head metadata stays UNKNOWN even if counts pass.
function verifyDiscoveredCI(evidence, expectedHead) {
  assert.match(expectedHead, /^[a-f0-9]{40}$/, 'expected head must be an exact SHA');
  assert.equal(evidence.schema, 'seatbelt-ci-discovery/v1');
  assert.equal(evidence.repo, 'stranske/Orchestrator');
  assert.equal(evidence.head_sha, expectedHead, 'discovered evidence covers a different head');
  assert.ok(typeof evidence.discovery_limit === 'string' && evidence.discovery_limit.trim(),
    'missing discovery limitation');
  assert.equal(evidence.runs.length, 2, 'wrong discovered workflow population');
  const seen = new Set();
  for (const run of evidence.runs) {
    assert.ok(Number.isSafeInteger(run.run_id) && run.run_id > 0, 'missing run identity');
    for (const job of run.jobs) {
      assert.ok(Number.isSafeInteger(job.id) && job.id > 0, 'missing job identity');
      assert.ok(!seen.has(job.id), 'duplicate job identity');
      seen.add(job.id);
      assert.equal(job.run_id, run.run_id, 'job run mismatch');
      assert.ok(job.status === 'completed' && ['success', 'skipped'].includes(job.conclusion),
        `failed or unfinished discovered job: ${job.name}`);
    }
  }
  const workflows = ['CI', 'Gate'].map(name => {
    const matches = evidence.runs.filter(run => run.name === name);
    assert.equal(matches.length, 1, `missing or duplicate discovered workflow: ${name}`);
    return matches[0];
  });
  const products = {
    'verify.py (real pytest counts + selftests + capability gates)': 'Verify',
    'verify.py in the flat exec-mirror shape (the tree a sync ships)':
      "Build the flat copy with the sync's copy contract and verify it there",
    'combined coverage (pytest + selftests, the honest number)': 'Measure combined coverage',
  };
  const measurements = [];
  for (const [name, stepName] of Object.entries(products)) {
    const jobs = workflows[0].jobs.filter(job => job.name === name);
    assert.equal(jobs.length, 1, `missing or duplicate discovered product job: ${name}`);
    const job = jobs[0];
    assert.equal(job.conclusion, 'success', `discovered product job did not execute: ${name}`);
    const steps = job.product_steps.filter(step => step.name === stepName);
    assert.equal(steps.length, 1, `missing discovered product step: ${stepName}`);
    assert.ok(steps[0].status === 'completed' && steps[0].conclusion === 'success',
      `discovered product step did not execute: ${stepName}`);
    if (name.startsWith('verify.py')) {
      const receipts = evidence.receipts.filter(receipt => receipt.job_id === job.id);
      assert.equal(receipts.length, 1, `missing or duplicate measurement receipt: ${name}`);
      measurements.push(verifyMeasurement(receipts[0], expectedHead, name));
    }
  }
  assert.equal(evidence.receipts.length, measurements.length, 'unbound measurement receipt');
  return { head_sha: expectedHead, measurements,
    complete_topology: 'UNKNOWN: first-page discovery lacks totals and per-job attempt/head bindings',
    hosted_acceptance: 'UNKNOWN: requires complete exact-head CI and Gate evidence' };
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
    measurements.push(verifyMeasurement(receipts[0], expectedHead, name));
  }
  return { head_sha: expectedHead, product_jobs: executed.size, measurements };
}

module.exports = { verifyProof, verifyOriginalCI, verifyHostedCI, verifyDiscoveredCI };
if (require.main === module) {
  Promise.all([verifyProof(), verifyOriginalCI()]).then(([result, original]) => {
    const args = process.argv.slice(2);
    assert.ok(args.length === 0 || args.length === 2,
      'usage: node scripts/verify_seatbelt_receipts.js [ci-snapshot.json expected-head-sha]');
    const snapshot = args.length ? JSON.parse(fs.readFileSync(args[0], 'utf8')) : undefined;
    const discovered = snapshot?.schema === 'seatbelt-ci-discovery/v1'
      ? verifyDiscoveredCI(snapshot, args[1]) : undefined;
    const hosted = snapshot && !discovered ? verifyHostedCI(snapshot, args[1]) : undefined;
    console.log(JSON.stringify({ retained_native_evidence: 'VERIFIED', ...result,
      retained_original_ci: original,
      ...(hosted ? { retained_hosted_ci: hosted } : {}),
      ...(discovered ? { retained_discovered_ci: discovered } : {}) }));
  }).catch((error) => {
    console.error(`retained evidence: NOT VERIFIED: ${error.message}`);
    process.exitCode = 1;
  });
}
