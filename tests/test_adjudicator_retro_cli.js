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
    assert.equal(result.complete, true);
    assert.deepEqual(result.gaps, []);
    assert.equal(result.repository, fs.realpathSync(checkout));
    assert.equal(result.evaluated_sha, evaluated);
    assert.deepEqual(result.sources, []);
    assert.equal(result.acceptance_artifacts.length, 1);
    assert.deepEqual(result.acceptance_artifacts[0], {
      path: artifactPath, evaluated_sha: evaluated, complete: true,
      blob_sha: blob, byte_length: raw.length,
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

const initializeDisputes = `
import json
import time
import feedback
with feedback._conn() as conn:
    for index in (1, 2):
        run_id = f"original-{index}"
        conn.execute(
            "INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)",
            (run_id, int(time.time()), f"owner/repo#{index}"),
        )
        conn.execute(
            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
            "VALUES (?,'NON_PASS','PASS',1)", (run_id,),
        )
print(json.dumps(True))
`;

test('retro dispatch routes each case and records shadow roles without writing outcomes', (t) => {
  const w = world(t);
  w.run(['-c', initializeDisputes]);
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
    const record = result.records[index];
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
});

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
