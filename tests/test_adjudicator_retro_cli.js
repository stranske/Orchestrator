'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;
const python = process.env.PYTHON || 'python3';

// Use the Brain's real schema and the default report path. All disputes have
// aged out, so this exercises report refresh without another paid adjudication.
const initialize = `
import json
import time
import adjudicator_retro as retro
import feedback

cases = [
    ("reject_blocker", "durable", 1.5, "ccusage"),
    ("uphold_blocker", "reverted", 2.5, "ccusage"),
    ("reject_blocker", "broke_later", 0, "ledger"),
    ("uphold_blocker", "pending", 0, "ccusage"),
    ("needs_more_evidence", "reverted", 0.5, "ccusage"),
    (None, "durable", None, None),
]
now = int(time.time())
rows = []
with feedback._conn() as conn:
    for index, (decision, durability, cost, source) in enumerate(cases):
        run_id = f"original-{index}"
        conn.execute(
            "INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)",
            (run_id, now - 100 * 86400, f"owner/repo#{index + 1}"),
        )
        conn.execute(
            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged,"
            "durability,durability_checked_ts) VALUES (?,'NON_PASS','PASS',1,?,?)",
            (run_id, durability, now),
        )
        row = {"case_id": run_id, "run_id": run_id, "merge_rule_verdict": "PASS"}
        if decision:
            backend = f"backend-{index}"
            conn.execute(
                "INSERT INTO costs(run_id,cost_usd,source) VALUES (?,?,?)",
                (backend, cost, source),
            )
            row.update(
                decision=decision,
                role_run_id=f"shadow-{index}",
                backend_run_id=backend,
                shadow_verdict={"uphold_blocker": "FAIL", "reject_blocker": "PASS"}.get(decision),
                later_truth=None,
                cost_usd=None,
            )
        else:
            row["error"] = "missing gate evidence"
        rows.append(row)
report = retro.report_path()
report.parent.mkdir(parents=True, exist_ok=True)
report.write_text(json.dumps({"rows": rows, "summary": {"cases": 999}}))
print(json.dumps(rows))
`;

const snapshot = `
import json
import feedback
with feedback._conn() as conn:
    print(json.dumps({table: conn.execute(f"SELECT * FROM {table} ORDER BY run_id").fetchall()
                      for table in ("runs", "outcomes", "costs")}))
`;

function world(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-retro-cli-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const env = {
    PATH: process.env.PATH,
    PYTHONPATH: modules,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_STATE_DIR: path.join(root, 'state'),
    ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
    ORCH_FEEDBACK_DB: path.join(root, 'brain.db'),
    ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
  };
  function run(args) {
    // File capture supports the same restricted runners as the other CLI witnesses.
    const output = path.join(root, 'stdout');
    const errors = path.join(root, 'stderr');
    const stdout = fs.openSync(output, 'w');
    const stderr = fs.openSync(errors, 'w');
    try {
      const result = spawnSync(python, args, {
        cwd: root, env, stdio: ['ignore', stdout, stderr], timeout: 10000,
      });
      assert.ifError(result.error);
      assert.equal(result.status, 0, fs.readFileSync(errors, 'utf8'));
      return JSON.parse(fs.readFileSync(output, 'utf8'));
    } finally {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
      fs.rmSync(output);
      fs.rmSync(errors);
    }
  }
  return {
    root,
    run,
    report: () => JSON.parse(fs.readFileSync(
      path.join(env.ORCH_STATE_DIR, 'capability-program', 'adjudicator-retro.json'), 'utf8',
    )),
    refresh: () => run([path.join(modules, 'adjudicator_retro.py'), '--dispatch', '--limit', '0']),
    brain: () => run(['-c', snapshot]),
  };
}

function localGit(w, checkout, ...args) {
  const output = path.join(w.root, 'git-stdout');
  const errors = path.join(w.root, 'git-stderr');
  const stdout = fs.openSync(output, 'w');
  const stderr = fs.openSync(errors, 'w');
  try {
    const result = spawnSync('git', ['-C', checkout, ...args], {
      env: { PATH: process.env.PATH }, stdio: ['ignore', stdout, stderr], timeout: 10000,
    });
    assert.ifError(result.error);
    assert.equal(result.status, 0, fs.readFileSync(errors, 'utf8'));
    return fs.readFileSync(output, 'utf8').trim();
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
    fs.rmSync(output);
    fs.rmSync(errors);
  }
}

for (const [name, artifactPath, content, executable] of [
  ['empty', 'artifacts/empty.log', '', false],
  ['unicode', 'artifacts/résumé\tresult\n.log', 'résultat: réussi ✓\n', false],
  ['CRLF', 'artifacts/windows.log', 'first\r\nsecond\r\n', false],
  ['executable without final newline', 'artifacts/check.sh', '#!/bin/sh\nexit 0', true],
  ['embedded NUL', 'artifacts/nul.log', 'first\0second\n', false],
]) {
  test(`collection CLI preserves exact ${name} artifact bytes and provenance`, (t) => {
    const w = world(t);
    const checkout = path.join(w.root, 'checkout');
    fs.mkdirSync(path.join(checkout, 'artifacts'), { recursive: true });
    const git = (...args) => localGit(w, checkout, ...args);
    git('init', '-q');
    git('config', 'user.name', 'Collector Test');
    git('config', 'user.email', 'collector@example.com');
    // Prevent inherited text conversion rules from changing our expected Git bytes.
    git('config', 'core.autocrlf', 'false');
    const raw = Buffer.from(content, 'utf8');
    fs.writeFileSync(path.join(checkout, artifactPath), raw);
    git('add', '--', artifactPath);
    if (executable) git('update-index', '--chmod=+x', '--', artifactPath);
    git('commit', '-qm', 'evaluated artifact');
    const evaluated = git('rev-parse', 'HEAD');
    const blob = git('rev-parse', `${evaluated}:${artifactPath}`);
    assert.ok(git('ls-tree', evaluated, '--', artifactPath).startsWith(
      executable ? '100755 blob ' : '100644 blob ',
    ));
    // Neither the current branch nor a dirty worktree may supply acceptance bytes.
    fs.writeFileSync(path.join(checkout, artifactPath), 'new HEAD\n');
    git('commit', '-am', 'new artifact', '-q');
    fs.unlinkSync(path.join(checkout, artifactPath));
    const report = path.join(w.root, 'saved.json');
    const output = path.join(w.root, 'collection.json');
    const location = `git-path:${artifactPath}`;
    const criterion = 'declared artifact';
    const saved = JSON.stringify({ rows: [{
      case_id: 'exact-artifact', decision: 'uphold_blocker', cost_usd: 1.5,
      packet: { disputed_finding: { decision: { evaluated_sha: evaluated } } },
      collection_requirements: {
        source_paths: [], acceptance: [{ criterion, location }],
      },
    }] });
    fs.writeFileSync(report, saved);
    const summary = w.run([
      path.join(modules, 'adjudicator_retro.py'), '--collect-case', 'exact-artifact',
      '--report', report, '--output', output, '--repository', checkout,
      '--byte-limit', String(Math.max(1, raw.length)),
    ]);
    const result = JSON.parse(fs.readFileSync(output, 'utf8'));
    assert.equal(summary.complete, true);
    assert.equal(summary.completeness_scope, 'supplied_inventory_only');
    assert.equal(summary.inventory_exhaustiveness, 'unverified');
    assert.equal(summary.acceptance_semantics, 'unassessed');
    assert.equal(result.complete, true);
    assert.deepEqual(result.gaps, []);
    assert.equal(result.repository, fs.realpathSync(checkout));
    assert.equal(result.evaluated_sha, evaluated);
    assert.deepEqual(result.sources, []);
    assert.equal(result.acceptance_artifacts.length, 1);
    assert.deepEqual(result.acceptance_artifacts[0], {
      path: artifactPath, evaluated_sha: evaluated, complete: true,
      blob_sha: blob, byte_length: raw.length,
      git_mode: executable ? '100755' : '100644',
      sha256: createHash('sha256').update(raw).digest('hex'), bytes_utf8: content,
      criterion, location, inventory_index: 0,
    });
    assert.deepEqual(Buffer.from(result.acceptance_artifacts[0].bytes_utf8, 'utf8'), raw);
    assert.equal(result.completeness_scope, 'supplied_inventory_only');
    assert.equal(result.inventory_exhaustiveness, 'unverified');
    assert.equal(result.acceptance_semantics, 'unassessed');
    assert.equal(fs.readFileSync(report, 'utf8'), saved);
    assert.equal(fs.existsSync(path.join(w.root, 'brain.db')), false);
  });
}

test('collection CLI reports every unresolved inventory slot alongside collected bytes', (t) => {
  const w = world(t);
  const checkout = path.join(w.root, 'checkout');
  fs.mkdirSync(path.join(checkout, 'artifacts'), { recursive: true });
  const git = (...args) => localGit(w, checkout, ...args);
  git('init', '-q');
  git('config', 'user.name', 'Collector Test');
  git('config', 'user.email', 'collector@example.com');
  fs.writeFileSync(path.join(checkout, 'artifacts/valid.log'), 'ok\n');
  fs.writeFileSync(path.join(checkout, 'artifacts/oversized.log'), 'too large\n');
  fs.writeFileSync(path.join(checkout, 'artifacts/invalid.bin'), Buffer.from([0xff, 0x00]));
  fs.symlinkSync('valid.log', path.join(checkout, 'artifacts/link.log'));
  git('add', '.');
  git('commit', '-qm', 'artifacts');
  // A gitlink is present in the inventory's commit but is not a regular blob.
  git('update-index', '--add', '--cacheinfo', `160000,${git('rev-parse', 'HEAD')},submodule`);
  git('commit', '-qm', 'gitlink');
  const evaluated = git('rev-parse', 'HEAD');
  const valid = { criterion: 'declared artifact', location: 'git-path:artifacts/valid.log' };
  const failures = [
    ['git-path:artifacts/missing.log', 'missing_source_object'],
    ['git-path:../outside.log', 'unsafe_source_path'],
    ['git-path:/absolute.log', 'unsafe_source_path'],
    ['git-path:', 'unsafe_source_path'],
    ['git-path:artifacts/oversized.log', 'source_byte_limit_exceeded'],
    ['git-path:artifacts/invalid.bin', 'unsupported_source_encoding'],
    ['git-path:artifacts/link.log', 'missing_source_object'],
    ['git-path:artifacts', 'missing_source_object'],
    ['git-path:submodule', 'missing_source_object'],
    ['git-path:artifacts/*.log', 'missing_source_object'],
    ['artifact:run/1', 'unsupported_acceptance_transport'],
  ];
  // Identical declarations must each retain their own inventory index. A valid
  // artifact and source must not satisfy another slot sharing the same criterion.
  const acceptance = [valid, { ...valid }, ...failures.map(([location]) => ({
    criterion: valid.criterion, location,
  }))];
  const saved = JSON.stringify({ rows: [{
    case_id: 'partial-inventory', decision: 'reject_blocker', cost_usd: 1.5,
    packet: { disputed_finding: { decision: { evaluated_sha: evaluated } } },
    collection_requirements: { source_paths: ['artifacts/valid.log'], acceptance },
  }] });
  const report = path.join(w.root, 'saved.json');
  const output = path.join(w.root, 'collection.json');
  fs.writeFileSync(report, saved);
  const summary = w.run([
    path.join(modules, 'adjudicator_retro.py'), '--collect-case', 'partial-inventory',
    '--report', report, '--output', output, '--repository', checkout, '--byte-limit', '4',
  ]);
  const result = JSON.parse(fs.readFileSync(output, 'utf8'));
  assert.equal(summary.complete, false);
  assert.equal(summary.completeness_scope, 'supplied_inventory_only');
  assert.equal(summary.inventory_exhaustiveness, 'unverified');
  assert.equal(summary.acceptance_semantics, 'unassessed');
  assert.deepEqual(summary.gaps, result.gaps);
  assert.equal(result.complete, false);
  assert.equal(result.sources[0].complete, true);
  assert.equal(result.acceptance_artifacts.length, acceptance.length);
  assert.deepEqual(result.acceptance_artifacts.map((record) => record.complete),
    acceptance.map((_item, index) => index < 2));
  assert.deepEqual(result.gaps.filter((gap) => gap.kind === 'missing_acceptance_evidence')
    .map((gap) => gap.criterion), acceptance.slice(2));
  for (const [index, record] of result.acceptance_artifacts.entries()) {
    assert.equal(record.inventory_index, index);
    assert.equal(record.criterion, acceptance[index].criterion);
    assert.equal(record.location, acceptance[index].location);
    assert.equal(record.evaluated_sha, evaluated);
    if (index < 2) {
      assert.equal(record.bytes_utf8, 'ok\n');
      assert.equal(record.blob_sha, git('rev-parse', `${evaluated}:artifacts/valid.log`));
    } else {
      const [location, kind] = failures[index - 2];
      assert.ok(result.gaps.some((gap) => gap.kind === kind && (
        gap.path === location.slice('git-path:'.length) || gap.location === location
      )), `missing reason for ${location}`);
      assert.equal(record.bytes_utf8, undefined);
    }
  }
  const oversized = result.acceptance_artifacts[6];
  assert.equal(oversized.byte_length, Buffer.byteLength('too large\n'));
  assert.equal(oversized.sha256, undefined, 'oversized bytes must not be read');
  assert.equal(result.completeness_scope, 'supplied_inventory_only');
  assert.equal(result.inventory_exhaustiveness, 'unverified');
  assert.equal(result.acceptance_semantics, 'unassessed');
  assert.equal(fs.readFileSync(report, 'utf8'), saved);
  assert.equal(fs.existsSync(path.join(w.root, 'brain.db')), false);
});

test('collection CLI rejects inventory claims that forge artifact provenance or completeness', (t) => {
  const w = world(t);
  const checkout = path.join(w.root, 'checkout');
  fs.mkdirSync(checkout);
  const git = (...args) => localGit(w, checkout, ...args);
  git('init', '-q');
  git('config', 'user.name', 'Collector Test');
  git('config', 'user.email', 'collector@example.com');
  const content = 'evaluated evidence\n';
  fs.writeFileSync(path.join(checkout, 'proof.log'), content);
  git('add', 'proof.log');
  git('commit', '-qm', 'evaluated artifact');
  const evaluated = git('rev-parse', 'HEAD');
  const blob = git('rev-parse', `${evaluated}:proof.log`);
  const forged = {
    complete: true, evaluated_sha: 'a'.repeat(40), path: 'proof.log',
    blob_sha: 'b'.repeat(40), byte_length: 1, sha256: 'c'.repeat(64),
    bytes_utf8: 'invented evidence', inventory_index: 99,
  };
  const acceptance = ['git-path:proof.log', 'git-path:missing.log', 'https://example.com/proof']
    .map((location) => ({ criterion: 'declared artifact', location, ...forged }));
  const report = path.join(w.root, 'saved.json');
  const output = path.join(w.root, 'collection.json');
  const saved = JSON.stringify({ rows: [{
    case_id: 'forged-inventory', decision: 'reject_blocker', shadow_verdict: 'PASS', cost_usd: 1.5,
    packet: { disputed_finding: { decision: { evaluated_sha: evaluated } } },
    collection_requirements: { source_paths: [], acceptance },
  }] });
  fs.writeFileSync(report, saved);
  const summary = w.run([
    path.join(modules, 'adjudicator_retro.py'), '--collect-case', 'forged-inventory',
    '--report', report, '--output', output, '--repository', checkout,
  ]);
  const result = JSON.parse(fs.readFileSync(output, 'utf8'));
  assert.equal(summary.complete, false);
  assert.equal(result.complete, false);
  assert.deepEqual(summary.gaps, result.gaps);
  assert.deepEqual(result.requirements.acceptance, acceptance);
  assert.deepEqual(result.acceptance_artifacts, [
    {
      path: 'proof.log', evaluated_sha: evaluated, complete: true, blob_sha: blob,
      git_mode: '100644',
      byte_length: Buffer.byteLength(content),
      sha256: createHash('sha256').update(content).digest('hex'), bytes_utf8: content,
      criterion: 'declared artifact', location: acceptance[0].location, inventory_index: 0,
    },
    {
      path: 'missing.log', evaluated_sha: evaluated, complete: false,
      criterion: 'declared artifact', location: acceptance[1].location, inventory_index: 1,
    },
    {
      evaluated_sha: evaluated, complete: false,
      criterion: 'declared artifact', location: acceptance[2].location, inventory_index: 2,
    },
  ]);
  assert.deepEqual(result.gaps.filter((gap) => gap.kind === 'missing_acceptance_evidence')
    .map((gap) => gap.criterion), acceptance.slice(1));
  assert.ok(result.gaps.some((gap) => gap.kind === 'missing_source_object'
    && gap.path === 'missing.log'));
  assert.ok(result.gaps.some((gap) => gap.kind === 'unsupported_acceptance_transport'
    && gap.location === acceptance[2].location));
  assert.equal(result.completeness_scope, 'supplied_inventory_only');
  assert.equal(result.inventory_exhaustiveness, 'unverified');
  assert.equal(result.acceptance_semantics, 'unassessed');
  assert.equal(fs.readFileSync(report, 'utf8'), saved);
  assert.equal(fs.existsSync(path.join(w.root, 'brain.db')), false);
});

test('collection CLI leaves failed acceptance and inventory exhaustiveness unassessed', (t) => {
  const w = world(t);
  const checkout = path.join(w.root, 'checkout');
  fs.mkdirSync(checkout);
  const git = (...args) => localGit(w, checkout, ...args);
  git('init', '-q');
  git('config', 'user.name', 'Collector Test');
  git('config', 'user.email', 'collector@example.com');
  const content = 'FAIL: required regression failed\n0 passed, 1 failed\n';
  fs.writeFileSync(path.join(checkout, 'failure.log'), content);
  fs.writeFileSync(path.join(checkout, 'undeclared.log'), 'additional evidence\n');
  git('add', '.');
  git('commit', '-qm', 'failed and undeclared artifacts');
  const evaluated = git('rev-parse', 'HEAD');
  const report = path.join(w.root, 'saved.json');
  const output = path.join(w.root, 'collection.json');
  const saved = JSON.stringify({ rows: [{
    case_id: 'partial-failed-acceptance', decision: 'reject_blocker',
    shadow_verdict: 'PASS', cost_usd: 1.5,
    completeness_scope: 'exhaustive', inventory_exhaustiveness: 'verified',
    acceptance_semantics: 'passed',
    packet: { disputed_finding: { decision: { evaluated_sha: evaluated } } },
    collection_requirements: {
      source_paths: [], acceptance: [{
        criterion: 'required regression passed', location: 'git-path:failure.log',
        acceptance_semantics: 'passed', inventory_exhaustiveness: 'verified',
      }],
    },
  }] });
  fs.writeFileSync(report, saved);
  const summary = w.run([
    path.join(modules, 'adjudicator_retro.py'), '--collect-case', 'partial-failed-acceptance',
    '--report', report, '--output', output, '--repository', checkout,
  ]);
  assert.deepEqual(summary, {
    case_id: 'partial-failed-acceptance', complete: true,
    completeness_scope: 'supplied_inventory_only', inventory_exhaustiveness: 'unverified',
    acceptance_semantics: 'unassessed', gaps: [],
  });
  const result = JSON.parse(fs.readFileSync(output, 'utf8'));
  for (const [key, value] of Object.entries(summary)) assert.deepEqual(result[key], value);
  assert.equal(result.acceptance_artifacts.length, 1, 'only the supplied inventory is collected');
  assert.equal(result.acceptance_artifacts[0].bytes_utf8, content);
  assert.equal(result.acceptance_artifacts[0].sha256,
    createHash('sha256').update(content).digest('hex'));
  assert.equal(result.decision, undefined, 'collection must not produce an adjudication');
  assert.equal(result.shadow_verdict, undefined);
  assert.equal(fs.readFileSync(report, 'utf8'), saved, 'saved verdicts and costs stay unchanged');
  assert.equal(fs.existsSync(path.join(w.root, 'brain.db')), false);
});

const initializeDisputes = `
import json
import sys
import time
import feedback
now = int(time.time())
newest = int(sys.argv[1]) if len(sys.argv) > 1 else 1
same_target = len(sys.argv) > 2 and sys.argv[2] == "same-target"
with feedback._conn() as conn:
    for index in (1, 2):
        run_id = f"original-{index}"
        conn.execute(
            "INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)",
            (run_id, now - (index != newest), f"owner/repo#{1 if same_target else index}"),
        )
        conn.execute(
            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
            "VALUES (?,'NON_PASS','PASS',1)", (run_id,),
        )
print(json.dumps(True))
`;

function testShadowDispatch(t, newest, sameTarget = false) {
  const w = world(t);
  w.run(['-c', initializeDisputes, String(newest), sameTarget ? 'same-target' : 'distinct-targets']);
  const before = w.brain();
  const result = w.run(['-c', `
import json
from unittest.mock import Mock, patch
import adjudicator_retro as retro
import feedback
import roles

def evidence(row):
    return {
        "disputed_finding": {"body": f"Missing acceptance test for {row['target']}"},
        "ground_truth_evidence": {
            "diff_summary": f"Added test for {row['target']}",
            "gate_runs": [{"name": "Gate", "conclusion": "SUCCESS", "detailsUrl": "gate-run"}],
        },
    }

def offload(backend, prompt, **kwargs):
    proposal = {
        "decision": "uphold_blocker",
        "confidence": "high",
        "rationale": "The finding needs inspection.",
        "evidence_assessment": [{
            "claim": "Missing test", "status": "supported", "evidence_ref": "gate-run",
            "reason": "Inspect the gate evidence",
        }],
        "ground_truth_refs": ["gate-run"],
        "recommended_next_step": "Inspect the regression evidence",
        "evidence_gaps": [],
    }
    return {"run_id": f"backend-{backend}", "output": json.dumps(proposal), "exit": 0}

reader = Mock(side_effect=evidence)
with (
    patch.object(roles, "route_role", side_effect=[{"agent": "gemini"}, {"agent": "codex"}]) as route,
    patch.object(roles, "_role_capability_event"),
    patch.object(roles.dispatcher, "offload", side_effect=offload) as transport,
):
    dry = retro.run(dispatch=False, limit=1, evidence_reader=reader)
    with feedback._conn() as conn:
        dry_roles = conn.execute("SELECT run_id FROM runs WHERE role_name='adjudicator'").fetchall()
    first = retro.run(dispatch=True, retry=True, limit=1, evidence_reader=reader)
    second = retro.run(dispatch=True, limit=1, evidence_reader=reader)
    resumed = retro.run(dispatch=True, limit=2, evidence_reader=reader)
    routes = [{"args": call.args, "kwargs": call.kwargs} for call in route.call_args_list]
    calls = [{"args": call.args, "kwargs": call.kwargs} for call in transport.call_args_list]
with feedback._conn() as conn:
    records = [dict(zip(("run_id", "target", "agent", "source", "metadata"), row))
               for row in conn.execute(
                   "SELECT run_id,target,agent,source,decomposition FROM runs "
                   "WHERE role_name='adjudicator' ORDER BY target")]
print(json.dumps({
    "dry": dry, "dry_roles": dry_roles, "first": first, "second": second, "resumed": resumed,
    "routes": routes, "calls": calls, "reads": reader.call_count, "records": records,
}))
`]);
  assert.deepEqual(result.dry_roles, [], 'packet preparation must not record an invocation');
  assert.equal(result.dry.rows.length, 1);
  assert.equal(result.first.rows.length, 1, 'the invocation limit must bound paid calls');
  assert.equal(result.second.rows.length, 2);
  assert.equal(result.reads, 3, 'saved verdicts must not be collected or dispatched again');
  assert.deepEqual(result.resumed.rows, result.second.rows);
  assert.deepEqual(result.routes.map((call) => call.args), [['adjudicator'], ['adjudicator']]);
  assert.deepEqual(result.calls.map((call) => call.args[0]), ['gemini', 'codex']);
  assert.deepEqual(result.second.rows.map((row) => row.run_id),
    [`original-${newest}`, `original-${3 - newest}`]);
  assert.equal(new Set(result.second.rows.map((row) => row.case_id)).size, 2,
    'persisted disputes need separate case identities even when they share a PR');
  assert.deepEqual(result.second.rows.map((row) => row.target),
    sameTarget ? ['owner/repo#1', 'owner/repo#1'] :
      [`owner/repo#${newest}`, `owner/repo#${3 - newest}`], 'replay follows dispute recency');
  assert.deepEqual(result.records.map((record) => record.target),
    sameTarget ? ['owner/repo#1', 'owner/repo#1'] : ['owner/repo#1', 'owner/repo#2'],
    'Brain query sorts by target, independently of replay');
  const recordsById = new Map(result.records.map((record) => [record.run_id, record]));
  assert.equal(recordsById.size, 2, 'each paid invocation must have a distinct role run');
  for (const [index, call] of result.calls.entries()) {
    const packet = result.second.rows[index].packet;
    assert.ok(call.args[1].includes(packet.disputed_finding.body));
    assert.ok(call.args[1].includes(packet.ground_truth_evidence.diff_summary));
    assert.ok(call.args[1].includes('gate-run'));
    assert.equal(call.kwargs.isolate, true, 'retrospective transport must isolate execution');
    assert.equal(call.kwargs.mode, 'full');
    assert.equal(call.kwargs.timeout, 180);
    assert.equal(call.kwargs.cwd, path.dirname(path.join(
      w.root, 'state/capability-program/adjudicator-retro.json',
    )));
    const row = result.second.rows[index];
    const record = recordsById.get(row.role_run_id);
    assert.ok(record, 'every saved verdict must identify its own Brain role run');
    const metadata = JSON.parse(record.metadata);
    assert.equal(record.run_id, row.role_run_id);
    assert.equal(record.target, row.target);
    assert.equal(record.agent, call.args[0]);
    assert.equal(record.source, 'retrospective');
    assert.equal(metadata.source, 'retrospective');
    assert.equal(metadata.backend_run_id, row.backend_run_id);
    assert.deepEqual(metadata.proposal, row.proposal);
    assert.equal(metadata.action, 'needs_more_evidence');
    assert.equal(row.shadow_verdict, null);
  }
  const after = w.brain();
  assert.deepEqual(after.outcomes, before.outcomes, 'no original or new outcome may be written');
  assert.deepEqual(after.costs, before.costs);
  assert.deepEqual(after.runs.filter((row) => row[0].startsWith('original-')), before.runs);
  assert.equal(after.runs.length, before.runs.length + 2);
  assert.deepEqual(w.report(), result.resumed);
}

for (const newest of [1, 2]) {
  test(`retro dispatch records shadow roles without writing outcomes (newest PR #${newest})`,
    (t) => testShadowDispatch(t, newest));
  test(`retro replays each persisted dispute on the same PR (newest run ${newest})`,
    (t) => testShadowDispatch(t, newest, true));
}

test('retro retries unavailable routing without recording a verdict or changing outcomes', (t) => {
  const w = world(t);
  w.run(['-c', initializeDisputes]);
  const before = w.brain();
  const result = w.run(['-c', `
import json
from unittest.mock import Mock, patch
import adjudicator_retro as retro
import feedback
import roles

reader = Mock(return_value={
    "disputed_finding": {"body": "Missing acceptance test"},
    "ground_truth_evidence": {"diff_summary": "Added test", "gate_runs": ["gate-run"]},
})
proposal = {
    "decision": "needs_more_evidence", "confidence": "low",
    "rationale": "Inspect the acceptance evidence.",
    "evidence_assessment": [{"claim": "Missing test", "status": "insufficient",
                             "evidence_ref": "gate-run", "reason": "Inspect source"}],
    "ground_truth_refs": ["gate-run"], "recommended_next_step": "Inspect source",
    "evidence_gaps": ["Source unavailable"],
}
with (
    patch.object(roles, "route_role", side_effect=[None, {"agent": "gemini"}]) as route,
    patch.object(roles, "_role_capability_event"),
    patch.object(roles.dispatcher, "offload", return_value={
        "run_id": "backend-recovered", "output": json.dumps(proposal), "exit": 0,
    }) as transport,
):
    unavailable = retro.run(dispatch=True, limit=1, evidence_reader=reader)
    with feedback._conn() as conn:
        unavailable_roles = conn.execute(
            "SELECT run_id FROM runs WHERE role_name='adjudicator'").fetchall()
    unavailable_calls = transport.call_count
    # Explicit retry, with one case, must retry this saved failure before the next dispute.
    recovered = retro.run(dispatch=True, retry=True, limit=1, evidence_reader=reader)
    stable = retro.run(dispatch=True, retry=True, limit=0, evidence_reader=reader)
    routes = [call.args for call in route.call_args_list]
    calls = [{"args": call.args, "kwargs": call.kwargs} for call in transport.call_args_list]
with feedback._conn() as conn:
    records = conn.execute(
        "SELECT run_id,target,agent,source,decomposition FROM runs "
        "WHERE role_name='adjudicator'").fetchall()
print(json.dumps({
    "unavailable": unavailable, "unavailable_roles": unavailable_roles,
    "unavailable_calls": unavailable_calls, "recovered": recovered, "stable": stable,
    "routes": routes, "calls": calls, "reads": reader.call_count, "records": records,
}))
`]);
  const failed = result.unavailable.rows[0];
  assert.equal(result.unavailable.population, 2);
  assert.equal(result.unavailable.rows.length, 1);
  assert.ok(failed.errors.some((error) => error.includes('no eligible backend')));
  assert.equal(failed.role_run_id, null);
  assert.equal(failed.backend_run_id, null);
  assert.equal(failed.decision, undefined);
  assert.deepEqual(result.unavailable_roles, []);
  assert.equal(result.unavailable_calls, 0);
  assert.deepEqual(result.routes, [['adjudicator'], ['adjudicator']]);
  assert.equal(result.reads, 2);
  assert.equal(result.calls.length, 1, 'only capacity recovery may invoke the backend');
  assert.equal(result.calls[0].args[0], 'gemini');
  assert.equal(result.calls[0].kwargs.isolate, true);
  const recovered = result.recovered.rows[0];
  assert.equal(result.recovered.rows.length, 1, 'retry must respect the one-case limit');
  assert.equal(recovered.case_id, failed.case_id, 'recovery preserves persisted dispute identity');
  assert.equal(recovered.run_id, failed.run_id);
  assert.deepEqual(recovered.errors, []);
  assert.equal(recovered.decision, 'needs_more_evidence');
  assert.equal(recovered.backend_run_id, 'backend-recovered');
  assert.deepEqual(result.stable.rows, result.recovered.rows);
  assert.equal(result.records.length, 1);
  const [runId, target, agent, source, decomposition] = result.records[0];
  assert.equal(runId, recovered.role_run_id);
  assert.equal(target, recovered.target);
  assert.equal(agent, 'gemini');
  assert.equal(source, 'retrospective');
  assert.deepEqual(JSON.parse(decomposition).proposal, recovered.proposal);
  const after = w.brain();
  assert.deepEqual(after.outcomes, before.outcomes);
  assert.deepEqual(after.costs, before.costs);
  assert.deepEqual(after.runs.filter((row) => row[0].startsWith('original-')), before.runs);
  assert.equal(after.runs.length, before.runs.length + 1);
  assert.deepEqual(w.report(), result.stable);
});

test('retro bounded batches count paid abstentions and invalid responses outside agreement rates', (t) => {
  const w = world(t);
  w.run(['-c', `
import json
import time
import feedback
now = int(time.time())
with feedback._conn() as conn:
    for index in range(4):
        conn.execute(
            "INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)",
            (f"original-{index}", now - index, f"owner/repo#{index + 1}"),
        )
        conn.execute(
            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged,"
            "durability,durability_checked_ts) VALUES (?,'NON_PASS','PASS',1,'reverted',?)",
            (f"original-{index}", now),
        )
        conn.execute(
            "INSERT INTO costs(run_id,cost_usd,source) VALUES (?,?,'ccusage')",
            (f"backend-{index}", [1.5, 2.5, 0, 0.75][index]),
        )
print(json.dumps(True))
`]);
  const before = w.brain();
  const result = w.run(['-c', `
import json
from unittest.mock import patch
import adjudicator_retro as retro
import roles

decisions = ["uphold_blocker", "reject_blocker", "needs_more_evidence"]
def offload(backend, prompt, **kwargs):
    index = offload.calls
    offload.calls += 1
    proposal = {
        "decision": decisions[index] if index < 3 else None,
        "confidence": "low", "rationale": "Inspect the acceptance evidence.",
        "evidence_assessment": [{"claim": "Missing test", "status": "insufficient",
                                 "evidence_ref": "gate-run", "reason": "Inspect source"}],
        "ground_truth_refs": ["gate-run"],
        "recommended_next_step": "Inspect source", "evidence_gaps": ["Source unavailable"],
    }
    return {"run_id": f"backend-{index}", "exit": 0,
            "output": json.dumps(proposal) if index < 3 else "malformed response"}
offload.calls = 0

def evidence(row):
    return {
        "disputed_finding": {"body": f"Missing acceptance test for {row['target']}"},
        "ground_truth_evidence": {"diff_summary": "Added test", "gate_runs": ["gate-run"]},
    }

with (
    patch.object(roles, "route_role", return_value={"agent": "gemini"}) as route,
    patch.object(roles, "_role_capability_event"),
    patch.object(roles.dispatcher, "offload", side_effect=offload),
):
    first = retro.run(dispatch=True, limit=2, evidence_reader=evidence)
    second = retro.run(dispatch=True, limit=2, evidence_reader=evidence)
    resumed = retro.run(dispatch=True, limit=4, evidence_reader=evidence)
print(json.dumps({"first": first, "second": second, "resumed": resumed,
                  "calls": offload.calls, "routes": route.call_count}))
`]);
  assert.equal(result.first.population, 4);
  assert.equal(result.first.summary.cases, 2, 'the paid-call limit bounds report case counts');
  assert.equal(result.first.summary.cost_usd, 4);
  assert.equal(result.first.summary.cost_per_case, 2);
  assert.equal(result.first.proposal_comparison.compared, 2);
  assert.equal(result.calls, 4, 'resume must not retry saved invalid responses or abstentions');
  assert.equal(result.routes, 4);
  const report = result.resumed;
  assert.deepEqual(report.rows, result.second.rows);
  assert.deepEqual(w.report(), report, 'the default state report must match the returned report');
  assert.deepEqual(report.summary, {
    cases: 4, adjudicated: 0, proposed_decisions: 2, metadata_only_cases: 4,
    graded: 0, agree: 0, disagree: 0,
    agreement_rate: null, merge_rule_agreement_rate: null,
    cost_usd: 4.75, cost_measured_cases: 4, cost_per_case: 1.1875,
  });
  assert.deepEqual(report.proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 4, proposed_decisions: 2,
    compared: 2, pending_truth: 0, missing_merge_disposition: 0, abstained: 1, unassessed: 1,
    agree: 1, disagree: 1, agreement_rate: 0.5,
    merge_rule_agree: 0, merge_rule_disagree: 2, merge_rule_agreement_rate: 0,
    cost_usd: 4.75, cost_measured_cases: 4, cost_per_case: 1.1875,
  });
  assert.equal(report.rows[2].decision, 'needs_more_evidence');
  assert.equal(report.rows[2].cost_usd, 0, 'a measured zero still belongs in the cost denominator');
  assert.equal(report.rows[3].decision, undefined);
  assert.ok(report.rows[3].errors.some((error) => error.includes('could not parse')));
  assert.equal(report.rows[3].cost_usd, 0.75, 'a failed response still incurs its measured cost');
  const after = w.brain();
  assert.deepEqual(after.outcomes, before.outcomes);
  assert.deepEqual(after.costs, before.costs);
  assert.deepEqual(after.runs.filter((row) => row[0].startsWith('original-')), before.runs);
  assert.equal(after.runs.length, before.runs.length + 4);
});

const paginatedGateEvidence = `
import json
import subprocess
import sys
from unittest.mock import patch
import adjudicator_retro as retro
import feedback
import roles
import verifier_evidence

requested_scenario = sys.argv[1]
recovery = len(sys.argv) > 2 and sys.argv[2] == "recover"
requests = []
first_gate = {"name": "Gate / initial", "conclusion": "SUCCESS", "detailsUrl": "first-gate"}
last_gate = {"context": "gate / final", "state": "SUCCESS", "targetUrl": "last-gate"}

def response(args):
    fields = dict(arg.split("=", 1) for arg in args if "=" in arg)
    requests.append({key: fields[key] for key in ("owner", "name", "n", "cursor") if key in fields})
    assert fields["owner"] == "owner" and fields["name"] == "repo"
    number = int(fields["n"])
    scenario = requested_scenario if not recovery or number == 1 else "complete"
    head = "a" * 40
    index = int(fields["cursor"].removeprefix("page-")) + 1 if "cursor" in fields else 1
    if index > 1:
        assert fields["cursor"] == f"page-{index - 1}"
        assert "after:$cursor" in fields["query"]
        if scenario == "timeout":
            raise subprocess.TimeoutExpired("gh", 120)
        if scenario == "head-changed":
            head = "c" * 40
    nodes = [{"name": f"unrelated-check-{index}", "conclusion": "SUCCESS"}]
    if scenario != "no-gate":
        if index == 1:
            nodes.append(first_gate)
        if index == 3:
            nodes.append(last_gate)
    pr = {
        "headRefOid": head,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {
            "pageInfo": {"hasNextPage": scenario == "unbounded" or index < 3,
                         "endCursor": f"page-{index}"},
            "nodes": nodes,
        }}}}]},
    }
    if index == 1:
        decision = {
            "schema": verifier_evidence.MARKER, "repo": "owner/repo", "pr": number,
            "head_sha": head, "evaluated_sha": "b" * 40,
            "run_id": "123", "run_attempt": "1", "provider_verdicts": ["FAIL"],
            "ci_failed": False, "verdict": "NON_PASS",
        }
        if scenario == "stale-verifier-head":
            decision["head_sha"] = "c" * 40
        if scenario == "stale-verifier-merge":
            decision["evaluated_sha"] = "c" * 40
        if scenario == "wrong-verifier-pr":
            decision["pr"] = number + 1
        pr.update({
            "number": number, "state": "MERGED", "mergeCommit": {"oid": "b" * 40},
            "files": {"pageInfo": {"hasNextPage": scenario == "truncated-files"},
                      "nodes": [{"path": "tests/test_acceptance.py", "additions": 10, "deletions": 0}]},
            "comments": {
                "pageInfo": {"hasPreviousPage": scenario == "truncated-comments"},
                "nodes": [{
                    "body": "Missing acceptance test\\n"
                            f"<!-- {verifier_evidence.MARKER} {json.dumps(decision)} -->",
                    "url": f"https://github.com/owner/repo/pull/{number}#issuecomment-1",
                    "author": {"login": "github-actions[bot]"},
                }],
            },
        })
        if scenario == "untrusted-verifier-author":
            pr["comments"]["nodes"][0]["author"]["login"] = "contributor"
        if scenario == "newer-verifier-pass":
            # The newest run wins even when comments arrive out of run order.
            # The old NON_PASS marker must not revive a resolved dispute.
            newer = {**decision, "run_id": "124", "provider_verdicts": ["PASS"],
                     "verdict": "PASS"}
            original = pr["comments"]["nodes"][0]
            pr["comments"]["nodes"].insert(0, {
                **original, "url": original["url"].replace("issuecomment-1", "issuecomment-2"),
                "body": "Acceptance test is now present\\n"
                        f"<!-- {verifier_evidence.MARKER} {json.dumps(newer)} -->",
            })
        if scenario == "newer-attempt-pass":
            # A rerun resolves the dispute even when its workflow run id is unchanged.
            newer = {**decision, "run_attempt": "2", "provider_verdicts": ["PASS"],
                     "verdict": "PASS"}
            original = pr["comments"]["nodes"][0]
            pr["comments"]["nodes"].append({
                **original, "url": original["url"].replace("issuecomment-1", "issuecomment-2"),
                "body": "Acceptance test is now present\\n"
                        f"<!-- {verifier_evidence.MARKER} {json.dumps(newer)} -->",
            })
        if scenario in ("numeric-run-order", "numeric-attempt-order", "invalid-newer-pass",
                        "untrusted-newer-pass", "wrong-url-newer-pass"):
            original = pr["comments"]["nodes"][0]
            if scenario == "numeric-attempt-order":
                decision["run_attempt"] = "10"
                original["body"] = "Missing acceptance test\\n" + (
                    f"<!-- {verifier_evidence.MARKER} {json.dumps(decision)} -->"
                )
            other = {**decision, "provider_verdicts": ["PASS"], "verdict": "PASS"}
            if scenario == "numeric-run-order":
                other["run_id"] = "99"
            elif scenario == "numeric-attempt-order":
                other["run_attempt"] = "9"
            else:
                other["run_id"] = "124"
            if scenario == "invalid-newer-pass":
                # A claimed PASS contradicting failed CI is not a verifier decision.
                other["ci_failed"] = True
            comment = {
                **original, "url": original["url"].replace("issuecomment-1", "issuecomment-2"),
                "body": "Unrelated apparent PASS\\n"
                        f"<!-- {verifier_evidence.MARKER} {json.dumps(other)} -->",
            }
            if scenario == "untrusted-newer-pass":
                comment["author"] = {"login": "contributor"}
            if scenario == "wrong-url-newer-pass":
                comment["url"] = f"https://github.com/other/repo/pull/{number}#issuecomment-2"
            pr["comments"]["nodes"].append(comment)
    if scenario == "missing-initial-commit" and index == 1 or (
        scenario == "missing-paginated-commit" and index == 2
    ):
        pr["commits"]["nodes"] = []
    if scenario == "null-initial-rollup" and index == 1 or (
        scenario == "null-paginated-rollup" and index == 2
    ):
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
    return {"data": {"repository": {"pullRequest": pr}}}

proposal = {
    "decision": "needs_more_evidence", "confidence": "low",
    "rationale": "Gate statuses alone do not prove acceptance.",
    "evidence_assessment": [{"claim": "Missing test", "status": "insufficient",
                             "evidence_ref": "last-gate", "reason": "Inspect source"}],
    "ground_truth_refs": ["first-gate", "last-gate"],
    "recommended_next_step": "Inspect acceptance artifacts", "evidence_gaps": ["Source unavailable"],
}
lifecycle = []

def checkpoint(report, route, transport):
    with feedback._conn() as conn:
        lifecycle.append({
            "report": report, "routes": route.call_count, "calls": transport.call_count,
            "outcomes": conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall(),
            "role_ids": [row[0] for row in conn.execute(
                "SELECT run_id FROM runs WHERE role_name='adjudicator' ORDER BY run_id"
            )],
        })

with (
    patch.object(retro, "_gh_json", side_effect=response),
    patch.object(roles, "route_role", return_value={"agent": "gemini"}) as route,
    patch.object(roles, "_role_capability_event"),
    patch.object(roles.dispatcher, "offload", return_value={
        "run_id": "backend-pages", "output": json.dumps(proposal), "exit": 0,
    }) as transport,
):
    report = retro.run(dispatch=True, limit=2 if recovery else 1)
    if recovery:
        checkpoint(report, route, transport)
        checkpoint(retro.run(dispatch=True, limit=2), route, transport)
        requested_scenario = "complete"
        checkpoint(retro.run(dispatch=True, retry=True, limit=0), route, transport)
        checkpoint(retro.run(dispatch=True, retry=True, limit=1), route, transport)
        report = retro.run(dispatch=True, retry=True, limit=2)
        checkpoint(report, route, transport)
with feedback._conn() as conn:
    records = conn.execute(
        "SELECT run_id,source,decomposition FROM runs WHERE role_name='adjudicator'"
    ).fetchall()
print(json.dumps({
    "report": report, "requests": requests, "routes": route.call_count, "records": records,
    "lifecycle": lifecycle,
    "calls": [{"args": c.args, "kwargs": c.kwargs} for c in transport.call_args_list],
}))
`;

for (const [scenario, reads, error] of [
  ['complete', 3, null],
  ['head-changed', 2, /head changed/],
  ['timeout', 2, /timed out/],
  ['no-gate', 3, /no gate run/],
  ['unbounded', 20, /bounded 20-page read/],
  ['truncated-comments', 1, /truncated verifier comment or diff evidence/],
  ['truncated-files', 1, /truncated verifier comment or diff evidence/],
  ['stale-verifier-head', 1, /current merge-bound verifier decision missing or changed/],
  ['stale-verifier-merge', 1, /current merge-bound verifier decision missing or changed/],
  ['wrong-verifier-pr', 1, /current merge-bound verifier decision missing or changed/],
  ['untrusted-verifier-author', 1, /current merge-bound verifier decision missing or changed/],
  ['newer-verifier-pass', 1, /current merge-bound verifier decision missing or changed/],
  ['newer-attempt-pass', 1, /current merge-bound verifier decision missing or changed/],
  ['numeric-run-order', 3, null],
  ['numeric-attempt-order', 3, null],
  ['invalid-newer-pass', 3, null],
  ['untrusted-newer-pass', 3, null],
  ['wrong-url-newer-pass', 3, null],
]) {
  test(`retro collects complete gate pages before shadow dispatch: ${scenario}`, (t) => {
    const w = world(t);
    w.run(['-c', initializeDisputes]);
    const before = w.brain();
    const result = w.run(['-c', paginatedGateEvidence, scenario]);
    const row = result.report.rows[0];
    assert.equal(result.report.rows.length, 1);
    assert.equal(result.requests.length, reads);
    assert.ok(result.requests.every((request) => request.n === row.target.split('#')[1]));
    assert.deepEqual(w.report(), result.report);
    assert.deepEqual(w.brain().outcomes, before.outcomes, 'evidence reads never change outcomes');
    if (error) {
      assert.match(row.error, error);
      assert.equal(result.routes, 0, 'incomplete evidence must fail before backend selection');
      assert.deepEqual(result.calls, []);
      assert.deepEqual(result.records, []);
      assert.deepEqual(w.brain(), before, 'incomplete evidence must leave the entire Brain unchanged');
    } else {
      assert.equal(row.error, undefined);
      const packet = row.packet;
      assert.equal(packet.disputed_finding.ref,
        `https://github.com/${row.target.replace('#', '/pull/')}#issuecomment-1`,
        'the selected verifier decision must supply its own finding comment');
      assert.ok(packet.disputed_finding.body.startsWith('Missing acceptance test\n'));
      assert.equal(packet.disputed_finding.decision.run_id, '123');
      assert.equal(packet.disputed_finding.decision.run_attempt,
        scenario === 'numeric-attempt-order' ? '10' : '1');
      assert.equal(packet.disputed_finding.decision.verdict, 'NON_PASS');
      assert.equal(packet.ground_truth_evidence.head_sha, 'a'.repeat(40));
      assert.equal(packet.ground_truth_evidence.merge_sha, 'b'.repeat(40));
      assert.deepEqual(packet.ground_truth_evidence.gate_runs, [
        { name: 'Gate / initial', conclusion: 'SUCCESS', detailsUrl: 'first-gate' },
        { context: 'gate / final', state: 'SUCCESS', targetUrl: 'last-gate' },
      ], 'both CheckRun and StatusContext gate evidence must survive pagination');
      assert.equal(result.routes, 1);
      assert.equal(result.calls.length, 1);
      const call = result.calls[0];
      assert.equal(call.args[0], 'gemini');
      for (const text of ['Missing acceptance test', 'tests/test_acceptance.py', 'first-gate', 'last-gate']) {
        assert.ok(call.args[1].includes(text), `actual role prompt must include ${text}`);
      }
      assert.equal(call.kwargs.isolate, true);
      assert.equal(result.records.length, 1);
      assert.equal(result.records[0][0], row.role_run_id);
      assert.equal(result.records[0][1], 'retrospective');
      assert.deepEqual(JSON.parse(result.records[0][2]).proposal, row.proposal);
      assert.equal(row.disposition, 'needs_more_evidence');
      assert.equal(row.shadow_verdict, null);
      assert.equal(w.brain().runs.length, before.runs.length + 1);
    }
  });
}

for (const [scenario, reads] of [
  ['missing-initial-commit', 1],
  ['missing-paginated-commit', 2],
  ['null-initial-rollup', 1],
  ['null-paginated-rollup', 2],
]) {
  test(`retro saves Gate gaps, continues the batch and recovers only on retry: ${scenario}`, (t) => {
    const w = world(t);
    w.run(['-c', initializeDisputes, '1']);
    const before = w.brain();
    const result = w.run(['-c', paginatedGateEvidence, scenario, 'recover']);
    assert.equal(result.lifecycle.length, 5);
    const initial = result.lifecycle[0];
    assert.equal(initial.report.rows.length, 2, 'a Gate gap must not abort the next dispute');
    const [failed, successful] = initial.report.rows;
    assert.equal(failed.target, 'owner/repo#1');
    assert.match(failed.error, /complete gate evidence unavailable/);
    assert.equal(failed.packet, undefined, 'partial Gate evidence must not create a packet');
    assert.equal(failed.backend_run_id, undefined);
    assert.equal(failed.role_run_id, undefined);
    assert.equal(failed.decision, undefined);
    assert.equal(successful.target, 'owner/repo#2');
    assert.equal(successful.error, undefined);
    assert.equal(successful.decision, 'needs_more_evidence', JSON.stringify(successful));
    assert.equal(initial.routes, 1);
    assert.equal(initial.calls, 1);
    assert.deepEqual(initial.role_ids, [successful.role_run_id]);
    for (const checkpoint of result.lifecycle.slice(1, 3)) {
      assert.deepEqual(checkpoint.report.rows, initial.report.rows,
        'ordinary resume and zero-limit retry must preserve both cases');
      assert.equal(checkpoint.routes, 1);
      assert.equal(checkpoint.calls, 1);
      assert.deepEqual(checkpoint.role_ids, initial.role_ids);
    }
    const recovered = result.lifecycle[3];
    const repaired = recovered.report.rows[0];
    assert.equal(repaired.case_id, failed.case_id);
    assert.equal(repaired.error, undefined);
    assert.equal(repaired.decision, 'needs_more_evidence');
    assert.ok(repaired.role_run_id);
    assert.notEqual(repaired.role_run_id, successful.role_run_id);
    assert.deepEqual(recovered.report.rows[1], successful,
      'retry must not replace a successful paid role run');
    assert.equal(recovered.routes, 2);
    assert.equal(recovered.calls, 2);
    assert.equal(recovered.role_ids.length, 2);
    assert.deepEqual(result.lifecycle[4].report.rows, recovered.report.rows);
    assert.equal(result.routes, 2);
    assert.equal(result.calls.length, 2);
    assert.equal(result.records.length, 2);
    assert.deepEqual(result.requests.map((request) => request.n), [
      ...Array(reads).fill('1'), ...Array(3).fill('2'), ...Array(3).fill('1'),
    ], 'only the failed dispute may recollect its Gate evidence');
    for (const checkpoint of result.lifecycle) {
      assert.deepEqual(checkpoint.outcomes, before.outcomes);
      assert.equal(checkpoint.report.summary.adjudicated, 0);
      assert.equal(checkpoint.report.summary.graded, 0);
    }
    for (const [runId, source, decomposition] of result.records) {
      const row = result.report.rows.find((entry) => entry.role_run_id === runId);
      assert.ok(row);
      assert.equal(source, 'retrospective');
      assert.deepEqual(JSON.parse(decomposition).proposal, row.proposal);
    }
    assert.ok(result.calls.every((call) => call.args[0] === 'gemini' && call.kwargs.isolate));
    assert.deepEqual(w.report(), result.report);
    const after = w.brain();
    assert.deepEqual(after.outcomes, before.outcomes);
    assert.deepEqual(after.costs, before.costs);
    assert.equal(after.runs.length, before.runs.length + 2);
  });
}

test('retro packet failures never reach routing, dispatch or Brain role recording', (t) => {
  const w = world(t);
  w.run(['-c', initializeDisputes]);
  const before = w.brain();
  const result = w.run(['-c', `
import json
from unittest.mock import patch
import adjudicator_retro as retro
import feedback
import roles

row = {"target": "owner/repo#1"}
valid = {
    "disputed_finding": {"body": "Missing acceptance test"},
    "ground_truth_evidence": {"diff_summary": "Added test", "gate_runs": ["gate-run"]},
}
invalid = [
    {key: value for key, value in valid.items() if key != missing}
    for missing in ("disputed_finding", "ground_truth_evidence")
]
invalid.extend({**valid, "disputed_finding": finding} for finding in (
    {"ref": "verifier-comment"}, {"body": " "},
    {"body": '<!-- verifier-corpus-decision/v1 {"verdict":"NON_PASS"} -->'},
))
invalid.extend({**valid, "ground_truth_evidence": ground} for ground in (
    "merge=PASS", {"diff_summary": "Added test", "gate_runs": []},
    {"diff_summary": [], "gate_runs": ["gate-run"]},
))
errors = []
with (
    patch.object(roles, "route_role", side_effect=AssertionError("invalid packet reached router")),
    patch.object(roles.dispatcher, "offload", side_effect=AssertionError("invalid packet dispatched")),
    patch.object(feedback, "record_role_run", side_effect=AssertionError("invalid packet recorded")),
):
    try:
        retro.build_packet({}, valid)
    except ValueError as exc:
        missing_target = str(exc)
    else:
        raise AssertionError("missing target accepted")
    for packet in invalid:
        try:
            retro.build_packet(row, packet)
        except ValueError:
            pass
        else:
            raise AssertionError("incomplete evidence accepted")
        report = retro.run(
            dispatch=True, retry=True, limit=1, evidence_reader=lambda _row: packet,
        )
        errors.append(report["rows"][0].get("error"))
    prepared = retro.run(
        dispatch=False, retry=True, limit=1, evidence_reader=lambda _row: valid,
    )
print(json.dumps({"missing_target": missing_target, "errors": errors, "prepared": prepared}))
`]);
  assert.match(result.missing_target, /target/);
  assert.equal(result.errors.length, 8);
  assert.ok(result.errors.every((error) => typeof error === 'string' && error.length));
  assert.ok(result.errors.some((error) => error.includes('finding comment text')));
  assert.ok(result.errors.some((error) => error.includes('merged diff summary')));
  assert.ok(result.errors.some((error) => error.includes('gate runs')));
  assert.equal(result.prepared.rows.length, 1);
  assert.equal(result.prepared.rows[0].error, undefined, 'a repaired packet clears the old error');
  assert.equal(result.prepared.rows[0].packet.source, 'retrospective');
  assert.equal(result.prepared.rows[0].packet.metadata_only, true);
  assert.equal(result.prepared.summary.adjudicated, 0);
  assert.deepEqual(w.brain(), before, 'invalid attempts and dry preparation must leave Brain unchanged');
  assert.deepEqual(w.report(), result.prepared);
});

test('retro CLI preserves raw proposals without grading metadata and refreshes measured costs', (t) => {
  const w = world(t);
  const saved = w.run(['-c', initialize]);
  const before = w.brain();
  const summary = w.refresh();
  assert.deepEqual(summary, {
    cases: 6, adjudicated: 0, proposed_decisions: 4, metadata_only_cases: 6,
    graded: 0, agree: 0, disagree: 0,
    agreement_rate: null, merge_rule_agreement_rate: null,
    cost_usd: 4.5, cost_measured_cases: 4, cost_per_case: 1.125,
  });
  const report = w.report();
  assert.deepEqual(report.proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 6, proposed_decisions: 4,
    compared: 3, pending_truth: 1, missing_merge_disposition: 0, abstained: 1, unassessed: 1,
    agree: 2, disagree: 1, agreement_rate: 2 / 3,
    merge_rule_agree: 1, merge_rule_disagree: 2, merge_rule_agreement_rate: 1 / 3,
    cost_usd: 4.5, cost_measured_cases: 4, cost_per_case: 1.125,
  });
  assert.deepEqual(report.rows.map((row) => row.proposal_comparison), [
    { verdict: 'PASS', agrees: true, merge_rule_agrees: true },
    { verdict: 'FAIL', agrees: true, merge_rule_agrees: false },
    { verdict: 'PASS', agrees: false, merge_rule_agrees: false },
    { verdict: 'FAIL', agrees: null, merge_rule_agrees: null },
    { verdict: null, agrees: null, merge_rule_agrees: null },
    { verdict: null, agrees: null, merge_rule_agrees: null },
  ]);
  assert.deepEqual(report.summary, summary, 'the CLI and persisted report must agree');
  assert.equal(report.population, 0);
  assert.equal(report.shadow, true);
  assert.equal(report.source, 'retrospective');
  assert.deepEqual(report.rows.map((row) => row.role_run_id), saved.map((row) => row.role_run_id));
  assert.deepEqual(report.rows.map((row) => row.decision), saved.map((row) => row.decision));
  assert.ok(report.rows.every((row) => row.disposition === 'needs_more_evidence'));
  assert.ok(report.rows.every((row) => row.shadow_verdict === null));
  assert.equal(report.rows[0].raw_shadow_verdict, 'PASS');
  assert.equal(report.rows[1].raw_shadow_verdict, 'FAIL');
  assert.equal(report.rows[2].cost_usd, null, 'ledger placeholders are not measured costs');
  assert.equal(report.rows[3].cost_usd, 0, 'a measured zero still counts in the denominator');
  assert.equal(report.rows[3].later_truth, null, 'pending durability must stay ungraded');
  assert.deepEqual(w.brain(), before, 'refresh must not write outcomes or dispatch role runs');
  const { line, rendered } = w.run(['-c', `
import json
import adjudicator_retro as retro
import switch_review
line = retro.weekly_line()
rendered = switch_review.format_report({
    "review_days": 7, "raise_count": 0,
    "held_off": [], "on_but_idle": [], "unconditioned": [],
    "mirror_drift": {"status": "ok"}, "adjudicator_shadow": line,
})
print(json.dumps({"line": line, "rendered": rendered}))
`]);
  assert.equal(line, 'adjudicator shadow: cases 6, agree 0, disagree 0, cost 4.5 '
    + '(graded 0; costs measured 4)');
  assert.equal(rendered.split('\n').filter((text) => text === line).length, 1);
});

test('switch review reads and renders saved adjudicator evidence without replay or writes', (t) => {
  const w = world(t);
  const results = w.run(['-c', `
import json
from contextlib import ExitStack
from unittest.mock import patch
import adjudicator_retro as retro
import feedback
import roles
import runtime_ac_gate
import switch_review

path = retro.report_path()
path.parent.mkdir(parents=True)
summary = {
    "cases": 4, "agree": 2, "disagree": 1, "cost_usd": 1.25,
    "graded": 3, "cost_measured_cases": 4,
}
fixtures = [
    ("measured", json.dumps({"summary": summary})),
    ("unmeasured", json.dumps({"summary": {
        **summary, "cases": 0, "agree": 0, "disagree": 0,
        "graded": 0, "cost_usd": None, "cost_measured_cases": 0,
    }})),
    ("missing", None),
    ("malformed", "{broken"),
    ("incomplete", json.dumps({"summary": {"cases": 4}})),
]

def snapshot():
    return {str(p): p.read_bytes() for p in path.parent.parent.rglob("*") if p.is_file()}

results = []
with ExitStack() as stack:
    # Isolate unrelated weekly readers; leave the real retro reader and renderer active.
    stack.enter_context(patch.object(switch_review, "_capability_heartbeat"))
    stack.enter_context(patch.object(switch_review, "switch_states", return_value={
        "held_off": [], "on_but_idle": [],
    }))
    stack.enter_context(patch.object(runtime_ac_gate, "shadow_summary", return_value=None))
    for name, value in {
        "stale_runners": [], "adversarial_shape_population": {},
        "mirror_drift": {"status": "ok"}, "fleet_gates": {},
        "firing_regressions": {}, "_exploration_gate": {},
        "gate_expiry": None, "capacity_shed": None,
    }.items():
        stack.enter_context(patch.object(switch_review, name, return_value=value))
    # Record forbidden calls as well as raising: a swallowed exception cannot hide them.
    forbidden = [stack.enter_context(patch.object(module, name,
        side_effect=AssertionError("weekly summary must only read the saved report")))
        for module, name in [
            (retro, "run"), (retro, "disputes"), (retro, "fetch_evidence"),
            (roles, "run_adjudicator_agent"), (feedback, "_conn"),
        ]]
    for name, payload in fixtures:
        if payload is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(payload)
        before = snapshot()
        report = switch_review.review(now=1800000000, env={"ORCH_VALUE_CHAIN_MONITOR": "0"})
        rendered = switch_review.format_report(report)
        assert snapshot() == before, "weekly review changed saved state"
        assert all(mock.call_count == 0 for mock in forbidden), "weekly review replayed evidence"
        results.append({"fixture": name, "line": report["adjudicator_shadow"],
                        "rendered": rendered, "raise_count": report["raise_count"]})
print(json.dumps(results))
`]);
  assert.equal(results.length, 5);
  for (const result of results) {
    const expected = result.fixture === 'measured'
      ? 'adjudicator shadow: cases 4, agree 2, disagree 1, cost 1.25 (graded 3; costs measured 4)'
      : result.fixture === 'unmeasured'
        ? 'adjudicator shadow: cases 0, agree 0, disagree 0, cost UNKNOWN (graded 0; costs measured 0)'
        : 'adjudicator shadow: cases UNKNOWN, agree UNKNOWN, disagree UNKNOWN, cost UNKNOWN';
    assert.equal(result.line, expected, result.fixture);
    assert.equal(result.rendered.split('\n').filter((line) => line === expected).length, 1,
      result.fixture);
    assert.equal(result.raise_count, 0, 'shadow evidence must remain informational');
  }
});

test('retro CLI reuses saved cases when later durability and costs arrive', (t) => {
  const w = world(t);
  w.run(['-c', initialize]);
  w.refresh();
  const identities = w.report().rows.map((row) => [row.case_id, row.role_run_id]);
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE outcomes SET durability='durable' WHERE run_id='original-3'")
    conn.execute("UPDATE costs SET cost_usd=0.75,source='ccusage' WHERE run_id='backend-2'")
print(json.dumps(True))
`]);
  const before = w.brain();
  const summary = w.refresh();
  assert.deepEqual(summary, {
    cases: 6, adjudicated: 0, proposed_decisions: 4, metadata_only_cases: 6,
    graded: 0, agree: 0, disagree: 0,
    agreement_rate: null, merge_rule_agreement_rate: null,
    cost_usd: 5.25, cost_measured_cases: 5, cost_per_case: 1.05,
  });
  assert.deepEqual(w.report().summary, summary);
  assert.deepEqual(w.report().proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 6, proposed_decisions: 4,
    compared: 4, pending_truth: 0, missing_merge_disposition: 0, abstained: 1, unassessed: 1,
    agree: 2, disagree: 2, agreement_rate: 0.5,
    merge_rule_agree: 2, merge_rule_disagree: 2, merge_rule_agreement_rate: 0.5,
    cost_usd: 5.25, cost_measured_cases: 5, cost_per_case: 1.05,
  });
  assert.deepEqual(w.report().rows.map((row) => [row.case_id, row.role_run_id]), identities);
  assert.deepEqual(w.brain(), before);
  assert.deepEqual(w.refresh(), summary, 'a second process must not duplicate saved cases');
  assert.deepEqual(w.brain(), before);

  // Refresh baseline facts too: unknown or deleted outcomes cannot keep stale comparisons.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE outcomes SET merged=NULL WHERE run_id='original-0'")
    conn.execute("UPDATE outcomes SET merged=0 WHERE run_id='original-1'")
    conn.execute("DELETE FROM outcomes WHERE run_id='original-2'")
print(json.dumps(True))
`]);
  const changedBrain = w.brain();
  w.refresh();
  assert.deepEqual(w.report().proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 6, proposed_decisions: 4,
    compared: 2, pending_truth: 1, missing_merge_disposition: 1, abstained: 1, unassessed: 1,
    agree: 1, disagree: 1, agreement_rate: 0.5,
    merge_rule_agree: 2, merge_rule_disagree: 0, merge_rule_agreement_rate: 1,
    cost_usd: 5.25, cost_measured_cases: 5, cost_per_case: 1.05,
  });
  assert.equal(w.report().rows[0].merge_rule_verdict, null);
  assert.equal(w.report().rows[0].proposal_comparison.agrees, null);
  assert.equal(w.report().rows[1].merge_rule_verdict, 'FAIL');
  assert.equal(w.report().rows[2].later_truth, null);
  assert.deepEqual(w.brain(), changedBrain);
});

test('retro CLI refreshes late abstention costs after its outcome leaves the Brain', (t) => {
  const w = world(t);
  w.run(['-c', initialize]);
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE costs SET source='ledger' WHERE run_id='backend-4'")
print(json.dumps(True))
`]);
  w.refresh();
  const initial = w.report();
  const identities = initial.rows.map((row) => [row.case_id, row.role_run_id, row.decision]);
  assert.equal(initial.rows[4].decision, 'needs_more_evidence');
  assert.equal(initial.rows[4].cost_usd, null, 'an incomplete ledger cost stays unmeasured');
  assert.equal(initial.summary.cost_usd, 4);
  assert.equal(initial.summary.cost_measured_cases, 3);

  // The original dispute has already aged out of the replay window. Losing
  // its outcome must not stop late telemetry for the paid abstention.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("DELETE FROM outcomes WHERE run_id='original-4'")
print(json.dumps(True))
`]);
  for (const cost of [0, 3]) {
    w.run(['-c', `
import json
import sys
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE costs SET cost_usd=?,source='ccusage' WHERE run_id='backend-4'",
                 (float(sys.argv[1]),))
print(json.dumps(True))
`, String(cost)]);
    const before = w.brain();
    w.refresh();
    const report = w.report();
    const abstention = report.rows[4];
    assert.equal(abstention.cost_usd, cost);
    assert.equal(abstention.later_truth, null);
    assert.equal(abstention.merge_rule_verdict, null);
    assert.deepEqual(abstention.proposal_comparison,
      { verdict: null, agrees: null, merge_rule_agrees: null });
    const expectedCosts = {
      cost_usd: 4 + cost, cost_measured_cases: 4, cost_per_case: (4 + cost) / 4,
    };
    assert.deepEqual(report.summary, { ...initial.summary, ...expectedCosts });
    assert.deepEqual(report.proposal_comparison,
      { ...initial.proposal_comparison, ...expectedCosts },
      'late abstention telemetry must not enter either binary agreement denominator');
    assert.deepEqual(report.rows.map((row) => [row.case_id, row.role_run_id, row.decision]),
      identities, 'refresh preserves paid role identities and raw proposals');
    assert.deepEqual(w.brain(), before, 'refresh must not recreate outcomes or role runs');
    assert.equal(report.population, 0, 'saved telemetry refresh outlives the replay population');
  }
});

test('retro CLI refreshes late invalid-response costs without grading or redispatch', (t) => {
  const w = world(t);
  w.run(['-c', `
import json
import time
from unittest.mock import patch
import adjudicator_retro as retro
import feedback
import roles

with feedback._conn() as conn:
    conn.execute("INSERT INTO runs(run_id,ts,target) VALUES ('original',?,'owner/repo#1')",
                 (int(time.time()),))
    conn.execute("INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged,"
                 "durability,durability_checked_ts) VALUES "
                 "('original','NON_PASS','PASS',1,'reverted',?)", (int(time.time()),))
    conn.execute("INSERT INTO costs(run_id,cost_usd,source) VALUES ('backend-invalid',0,'ledger')")

with (
    patch.object(roles, "route_role", return_value={"agent": "gemini"}),
    patch.object(roles, "_role_capability_event"),
    patch.object(roles.dispatcher, "offload", return_value={
        "run_id": "backend-invalid", "exit": 0, "output": "malformed response",
    }) as offload,
):
    result = retro.run(dispatch=True, evidence_reader=lambda row: {
        "disputed_finding": {"body": "Missing acceptance test"},
        "ground_truth_evidence": {"diff_summary": "Added test", "gate_runs": ["gate-run"]},
    })
    assert offload.call_count == 1
print(json.dumps(result))
`]);
  const initial = w.report();
  const invalid = initial.rows[0];
  assert.equal(invalid.decision, undefined);
  assert.ok(invalid.errors.some((error) => error.includes('could not parse')));
  assert.ok(invalid.role_run_id, 'the paid invalid response has a retrospective Brain record');
  assert.equal(invalid.backend_run_id, 'backend-invalid');
  assert.equal(invalid.cost_usd, null);
  const identity = [invalid.case_id, invalid.role_run_id, invalid.backend_run_id];
  const withoutCosts = ({ cost_usd, cost_measured_cases, cost_per_case, ...counts }) => counts;

  // No replay population remains. Refresh must still account for paid attempts
  // with no usable decision, even after the original outcome disappears.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("DELETE FROM outcomes WHERE run_id='original'")
print(json.dumps(True))
`]);
  for (const [cost, source, expected] of [
    [0, 'ccusage', 0],
    [2, 'ccusage', 2],
    [7, 'ledger', null],
    [null, null, null],
    [3, 'ccusage', 3],
  ]) {
    w.run(['-c', `
import json
import sys
import feedback
cost, source = json.loads(sys.argv[1])
with feedback._conn() as conn:
    conn.execute("DELETE FROM costs WHERE run_id='backend-invalid'")
    if source is not None:
        conn.execute("INSERT INTO costs(run_id,cost_usd,source) VALUES ('backend-invalid',?,?)",
                     (cost, source))
print(json.dumps(True))
`, JSON.stringify([cost, source])]);
    const before = w.brain();
    w.refresh();
    const report = w.report();
    const saved = report.rows[0];
    assert.equal(saved.cost_usd, expected, `late cost source ${source} with value ${cost}`);
    for (const section of ['summary', 'proposal_comparison']) {
      assert.equal(report[section].cost_usd, expected);
      assert.equal(report[section].cost_per_case, expected);
      assert.equal(report[section].cost_measured_cases, expected === null ? 0 : 1);
      assert.deepEqual(withoutCosts(report[section]), withoutCosts(initial[section]),
        'invalid responses must stay outside both agreement denominators');
    }
    assert.deepEqual([saved.case_id, saved.role_run_id, saved.backend_run_id], identity);
    assert.equal(saved.decision, undefined);
    assert.equal(saved.shadow_verdict, null);
    assert.deepEqual(saved.errors, invalid.errors);
    assert.deepEqual(saved.proposal_comparison,
      { verdict: null, agrees: null, merge_rule_agrees: null });
    assert.deepEqual(w.brain(), before, 'refresh must not redispatch or recreate the outcome');
    assert.equal(report.population, 0);
  }
});

test('retro CLI withdraws stale costs without changing the comparison cohort', (t) => {
  const w = world(t);
  w.run(['-c', initialize]);
  w.refresh();
  const initial = w.report();
  const identities = initial.rows.map((row) => [row.case_id, row.role_run_id, row.decision]);

  function checkCosts(total, measured, average) {
    const before = w.brain();
    const summary = w.refresh();
    const report = w.report();
    assert.deepEqual(report.summary, summary);
    for (const section of [summary, report.proposal_comparison]) {
      assert.equal(section.cost_usd, total);
      assert.equal(section.cost_measured_cases, measured);
      assert.equal(section.cost_per_case, average);
    }
    const withoutCosts = (comparison) => {
      const { cost_usd, cost_measured_cases, cost_per_case, ...counts } = comparison;
      return counts;
    };
    assert.deepEqual(withoutCosts(report.proposal_comparison),
      withoutCosts(initial.proposal_comparison), 'cost provenance must not change agreement rates');
    assert.deepEqual(report.rows.map((row) => row.proposal_comparison),
      initial.rows.map((row) => row.proposal_comparison));
    assert.deepEqual(report.rows.map((row) => [row.case_id, row.role_run_id, row.decision]),
      identities, 'refresh must preserve saved proposals without redispatch');
    assert.deepEqual(w.brain(), before, 'cost refresh must keep Brain and outcomes read-only');
    return report;
  }

  // Withdrawing a cost's complete source must clear its saved measured value,
  // even if the replacement ledger value is nonzero.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE costs SET source='ledger' WHERE run_id='backend-0'")
    conn.execute("DELETE FROM costs WHERE run_id='backend-1'")
print(json.dumps(True))
`]);
  const partial = checkCosts(0.5, 2, 0.25);
  assert.equal(partial.rows[0].cost_usd, null);
  assert.equal(partial.rows[1].cost_usd, null);
  assert.equal(partial.rows[3].cost_usd, 0, 'measured zero belongs in the cost denominator');
  assert.equal(partial.rows[4].cost_usd, 0.5, 'abstaining still incurs a measured case cost');

  // A measured zero differs from the absence of any measured costs.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE costs SET source='ledger' WHERE run_id='backend-4'")
print(json.dumps(True))
`]);
  checkCosts(0, 1, 0);
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE costs SET source='ledger' WHERE run_id='backend-3'")
print(json.dumps(True))
`]);
  const unmeasured = checkCosts(null, 0, null);
  assert.ok(unmeasured.rows.every((row) => row.cost_usd == null));

  // Restoring complete telemetry recovers the original totals without another call.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE costs SET source='ccusage' WHERE run_id IN ('backend-0','backend-3','backend-4')")
    conn.execute("INSERT INTO costs(run_id,cost_usd,source) VALUES ('backend-1',2.5,'ccusage')")
print(json.dumps(True))
`]);
  const restored = checkCosts(4.5, 4, 1.125);
  assert.deepEqual(restored.proposal_comparison, initial.proposal_comparison);
});

test('retro CLI rebuilds stale comparison caches from decisions and current Brain evidence', (t) => {
  const w = world(t);
  w.run(['-c', initialize]);
  w.refresh();
  const initial = w.report();
  const before = w.brain();
  const identities = initial.rows.map((row) => [row.case_id, row.role_run_id, row.decision]);
  w.run(['-c', `
import json
import adjudicator_retro as retro
report_path = retro.report_path()
report = json.loads(report_path.read_text())
for row in report["rows"]:
    if row.get("decision"):
        # Legacy mirrors and cached comparisons disagree with the saved proposal.
        stale = "FAIL" if row["decision"] == "reject_blocker" else "PASS"
        row.update(raw_shadow_verdict=stale, shadow_verdict=stale,
                   later_truth="PASS", merge_rule_verdict="FAIL", cost_usd=999,
                   disposition="reject_blocker", metadata_only=False,
                   proposal_comparison={"verdict": stale, "agrees": True,
                                        "merge_rule_agrees": True})
report["summary"] = {"cases": 999, "agree": 999, "cost_usd": 999}
report["proposal_comparison"] = {"compared": 999, "agreement_rate": 1}
report_path.write_text(json.dumps(report))
print(json.dumps(True))
`]);
  w.refresh();
  const refreshed = w.report();
  assert.deepEqual(refreshed.summary, initial.summary);
  assert.deepEqual(refreshed.proposal_comparison, initial.proposal_comparison,
    'both rates and measured costs must be rebuilt rather than trusting saved aggregates');
  assert.deepEqual(refreshed.rows.map((row) => row.proposal_comparison),
    initial.rows.map((row) => row.proposal_comparison),
    'proposal decisions must override contradictory legacy verdict mirrors');
  assert.deepEqual(refreshed.rows.map((row) => [row.later_truth, row.merge_rule_verdict, row.cost_usd]),
    initial.rows.map((row) => [row.later_truth, row.merge_rule_verdict, row.cost_usd]),
    'judged durability, merge disposition and complete costs come from the current Brain');
  assert.deepEqual(refreshed.rows.map((row) => [row.case_id, row.role_run_id, row.decision]),
    identities, 'refresh must retain the original proposals and paid role identities');
  assert.equal(refreshed.rows[0].raw_shadow_verdict, 'FAIL', 'legacy evidence stays inspectable');
  assert.ok(refreshed.rows.every((row) => row.shadow_verdict === null));
  assert.ok(refreshed.rows.every((row) => row.metadata_only === true));
  assert.ok(refreshed.rows.every((row) => row.disposition === 'needs_more_evidence'));
  assert.equal(refreshed.population, 0, 'cache repair must work after disputes leave replay');
  assert.deepEqual(w.brain(), before, 'refresh must not rewrite outcomes, costs or role records');
});

test('retro CLI compares both rules on the same cases with mixed merge dispositions', (t) => {
  const w = world(t);
  w.run(['-c', initialize]);
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE outcomes SET merged=0 WHERE run_id='original-1'")
    conn.execute("UPDATE outcomes SET merged=NULL WHERE run_id='original-2'")
    conn.execute("UPDATE outcomes SET durability='reworked' WHERE run_id='original-3'")
print(json.dumps(True))
`]);
  const before = w.brain();
  const summary = w.refresh();
  const initial = w.report();
  assert.deepEqual(initial.summary, summary);
  assert.deepEqual(initial.proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 6, proposed_decisions: 4,
    compared: 3, pending_truth: 0, missing_merge_disposition: 1, abstained: 1, unassessed: 1,
    agree: 3, disagree: 0, agreement_rate: 1,
    merge_rule_agree: 2, merge_rule_disagree: 1, merge_rule_agreement_rate: 2 / 3,
    cost_usd: 4.5, cost_measured_cases: 4, cost_per_case: 1.125,
  });
  assert.deepEqual(initial.rows.map((row) => row.proposal_comparison), [
    { verdict: 'PASS', agrees: true, merge_rule_agrees: true },
    { verdict: 'FAIL', agrees: true, merge_rule_agrees: true },
    { verdict: 'PASS', agrees: null, merge_rule_agrees: null },
    { verdict: 'FAIL', agrees: true, merge_rule_agrees: false },
    { verdict: null, agrees: null, merge_rule_agrees: null },
    { verdict: null, agrees: null, merge_rule_agrees: null },
  ]);
  assert.deepEqual(w.brain(), before);

  // Clearing the last missing merge fact admits the case to BOTH denominators.
  // Its zero-dollar ledger entry must still stay outside measured-cost counts.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE outcomes SET merged=0 WHERE run_id='original-2'")
print(json.dumps(True))
`]);
  const changedBrain = w.brain();
  w.refresh();
  const compared = w.report();
  assert.deepEqual(compared.proposal_comparison, {
    ...initial.proposal_comparison,
    compared: 4, missing_merge_disposition: 0,
    agree: 3, disagree: 1, agreement_rate: 0.75,
    merge_rule_agree: 3, merge_rule_disagree: 1, merge_rule_agreement_rate: 0.75,
  });
  assert.deepEqual(compared.rows[2].proposal_comparison, {
    verdict: 'PASS', agrees: false, merge_rule_agrees: true,
  });
  assert.deepEqual(compared.rows.map((row) => [row.case_id, row.role_run_id, row.decision]),
    initial.rows.map((row) => [row.case_id, row.role_run_id, row.decision]));
  assert.deepEqual(compared.summary, initial.summary,
    'raw proposal comparisons must not upgrade effective verdicts or change costs');
  assert.deepEqual(w.brain(), changedBrain, 'comparison refresh must not write any Brain table');
});

test('retro CLI regrades saved durable cases when later failure signals arrive', (t) => {
  const w = world(t);
  w.run(['-c', initialize]);
  w.refresh();
  const initial = w.report();
  const identities = initial.rows.map((row) => [row.case_id, row.role_run_id, row.decision]);

  // A prior PASS observation must not mask a subsequent failure. Exercise each
  // fleet failure signal independently, retaining the merge disposition and costs.
  for (const durability of ['reverted', 'broke_later', 'reopened', 'abandoned', 'reworked']) {
    w.run(['-c', `
import json
import sys
import feedback
with feedback._conn() as conn:
    conn.execute(
        "UPDATE outcomes SET durability=? WHERE run_id='original-0'",
        (sys.argv[1],),
    )
print(json.dumps(True))
`, durability]);
    const changedBrain = w.brain();
    w.refresh();
    const failed = w.report();
    assert.equal(failed.rows[0].later_truth, 'FAIL', durability);
    assert.deepEqual(failed.rows[0].proposal_comparison, {
      verdict: 'PASS', agrees: false, merge_rule_agrees: false,
    });
    assert.deepEqual(failed.proposal_comparison, {
      ...initial.proposal_comparison,
      agree: 1, disagree: 2, agreement_rate: 1 / 3,
      merge_rule_agree: 0, merge_rule_disagree: 3, merge_rule_agreement_rate: 0,
    }, durability);
    assert.deepEqual(failed.summary, initial.summary);
    assert.deepEqual(failed.rows.map((row) => [row.case_id, row.role_run_id, row.decision]),
      identities);
    assert.ok(failed.rows.every((row) => row.shadow_verdict === null));
    assert.deepEqual(w.brain(), changedBrain, 'regrading must leave the entire Brain unchanged');
  }

  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE outcomes SET durability='durable' WHERE run_id='original-0'")
print(json.dumps(True))
`]);
  const restoredBrain = w.brain();
  w.refresh();
  assert.deepEqual(w.report().proposal_comparison, initial.proposal_comparison);
  assert.deepEqual(w.report().rows.map((row) => [row.case_id, row.role_run_id, row.decision]),
    identities);
  assert.deepEqual(w.brain(), restoredBrain, 'recovering truth must not redispatch saved cases');
});

test('retro CLI publishes an empty report without inventing agreement or cost', (t) => {
  const w = world(t);
  w.run(['-c', 'import json, feedback; feedback._conn().close(); print(json.dumps(True))']);
  const before = w.brain();
  const summary = w.refresh();
  assert.deepEqual(summary, {
    cases: 0, adjudicated: 0, proposed_decisions: 0, metadata_only_cases: 0,
    graded: 0, agree: 0, disagree: 0,
    agreement_rate: null, merge_rule_agreement_rate: null,
    cost_usd: null, cost_measured_cases: 0, cost_per_case: null,
  });
  assert.deepEqual(w.report().summary, summary);
  assert.deepEqual(w.report().rows, []);
  assert.deepEqual(w.report().proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 0, proposed_decisions: 0,
    compared: 0, pending_truth: 0, missing_merge_disposition: 0, abstained: 0, unassessed: 0,
    agree: 0, disagree: 0, agreement_rate: null,
    merge_rule_agree: 0, merge_rule_disagree: 0, merge_rule_agreement_rate: null,
    cost_usd: null, cost_measured_cases: 0, cost_per_case: null,
  });
  assert.deepEqual(w.brain(), before);
});

test('retro CLI withdraws saved comparisons when their outcome is missing', (t) => {
  const w = world(t);
  w.run(['-c', initialize]);
  w.refresh();
  const initial = w.report();
  const identities = initial.rows.map((row) => [row.case_id, row.role_run_id, row.decision]);
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("DELETE FROM outcomes WHERE run_id='original-0'")
print(json.dumps(True))
`]);
  const before = w.brain();
  w.refresh();
  const report = w.report();
  assert.equal(report.rows[0].later_truth, null, 'a missing outcome cannot retain durable truth');
  assert.equal(report.rows[0].merge_rule_verdict, null, 'a missing outcome has no merge baseline');
  assert.deepEqual(report.rows[0].proposal_comparison, {
    verdict: 'PASS', agrees: null, merge_rule_agrees: null,
  });
  assert.deepEqual(report.proposal_comparison, {
    ...initial.proposal_comparison,
    compared: 2, pending_truth: 2, agree: 1, disagree: 1, agreement_rate: 0.5,
    merge_rule_agree: 0, merge_rule_disagree: 2, merge_rule_agreement_rate: 0,
  }, 'both agreement rates must remove the missing case from their denominator');
  assert.deepEqual(report.summary, initial.summary, 'paid costs survive loss of later truth');
  assert.deepEqual(report.rows.map((row) => [row.case_id, row.role_run_id, row.decision]),
    identities, 'withdrawal preserves saved proposals and role-run identities');
  assert.deepEqual(w.brain(), before, 'report refresh must not recreate the missing outcome');
});

test('retro CLI removes stale comparisons until durability is judged again', (t) => {
  const w = world(t);
  w.run(['-c', initialize]);
  w.refresh();
  const initial = w.report();
  const identities = initial.rows.map((row) => [row.case_id, row.role_run_id]);

  // Retain the durable/reverted labels, but withdraw their trusted observation dates.
  // A previously graded report must not keep counting these labels as later truth.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute(
        "UPDATE outcomes SET durability_checked_ts=? WHERE run_id='original-0'",
        (feedback.DURABILITY_DETECTION_SINCE - 1,),
    )
    conn.execute("UPDATE outcomes SET durability_checked_ts=NULL WHERE run_id='original-1'")
print(json.dumps(True))
`]);
  const unjudgedBrain = w.brain();
  w.refresh();
  const unjudged = w.report();
  assert.deepEqual(unjudged.proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 6, proposed_decisions: 4,
    compared: 1, pending_truth: 3, missing_merge_disposition: 0, abstained: 1, unassessed: 1,
    agree: 0, disagree: 1, agreement_rate: 0,
    merge_rule_agree: 0, merge_rule_disagree: 1, merge_rule_agreement_rate: 0,
    cost_usd: 4.5, cost_measured_cases: 4, cost_per_case: 1.125,
  });
  for (const index of [0, 1]) {
    assert.equal(unjudged.rows[index].later_truth, null);
    assert.deepEqual(unjudged.rows[index].proposal_comparison, {
      verdict: index === 0 ? 'PASS' : 'FAIL', agrees: null, merge_rule_agrees: null,
    });
  }
  assert.deepEqual(unjudged.summary, initial.summary);
  assert.deepEqual(w.brain(), unjudgedBrain, 'regrading must leave the Brain unchanged');

  // The detection boundary itself is trusted. Regrading restores the original
  // comparison without paying for another verdict or recreating any role run.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute(
        "UPDATE outcomes SET durability_checked_ts=? WHERE run_id IN ('original-0','original-1')",
        (feedback.DURABILITY_DETECTION_SINCE,),
    )
print(json.dumps(True))
`]);
  const rejudgedBrain = w.brain();
  w.refresh();
  const restored = w.report();
  assert.deepEqual(restored.proposal_comparison, initial.proposal_comparison);
  assert.deepEqual(restored.rows.map((row) => [row.case_id, row.role_run_id]), identities);
  assert.ok(restored.rows.every((row) => row.shadow_verdict === null));
  assert.deepEqual(w.brain(), rejudgedBrain, 'restoring truth must not redispatch saved cases');
});

test('closer lane offers the adjudicator only when both verdicts establish a dispute', (t) => {
  const w = world(t);
  const results = w.run(['-c', `
import json
from unittest.mock import patch
import capabilities
import capability_advisor as advisor

row = capabilities._blank_capability("role-adjudicator")
row["status"] = "generated"
capabilities.save({"role-adjudicator": row})
contexts = [
    {},
    {"verifier_verdict": "NON_PASS"},
    {"merge_disposition": "PASS"},
    {"verifier_verdict": "UNKNOWN", "merge_disposition": "PASS"},
    {"verifier_verdict": "NON_PASS", "merge_disposition": " UNKNOWN "},
    {"verifier_verdict": " ", "merge_disposition": "PASS"},
    {"verifier_verdict": "pass", "merge_disposition": " PASS "},
    {"verifier_verdict": "NON_PASS", "merge_disposition": "non_pass"},
    {"verifier_verdict": "NON_PASS", "merge_disposition": "PASS"},
    {"verifier_verdict": "PASS", "merge_disposition": " NON_PASS "},
]
results = []
with patch.object(advisor, "PR_FACTS_FETCH", return_value={}):
    for text in ("xyzzy plugh", "review disputed verifier results"):
        for context in contexts:
            result = advisor.advise(
                text, surface="closer-lane", repository="owner/repo",
                context=context, record=False,
            )
            results.append({
                "text": text,
                "context": context,
                "offered": any(c["capability_id"] == "role-adjudicator"
                               for c in result["capabilities"]),
                "withheld": result["precondition"]["withheld"],
                "missing": result["fact_missing"],
            })
print(json.dumps(results))
`]);
  assert.equal(results.length, 20);
  for (const [index, result] of results.entries()) {
    const offered = index % 10 >= 8;
    assert.equal(result.offered, offered, JSON.stringify(result));
    assert.equal(Boolean(result.withheld.length || result.missing.length), !offered,
      'an ineligible offer must explain whether evidence is missing or verdicts agree');
  }
});
