'use strict';

// Synthetic CLI witnesses only; these are not production acceptance receipts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { DatabaseSync } = require('node:sqlite');
const { reportWindow } = require('../scripts/report_relay_join_window');

const repo = path.resolve(__dirname, '..');
const moduleDir = fs.existsSync(path.join(repo, 'src', 'rate_incidents.py'))
  ? path.join(repo, 'src') : repo;
const python = process.env.PYTHON || 'python3';
const deployment = 1793693792; // 2026-11-03T08:16:32Z, the recorded deployment boundary.
const deadline = deployment + 24 * 60 * 60;

function world(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-acceptance-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const log = path.join(root, 'relay-pacing.log');
  const tail = path.join(root, 'tail');
  fs.writeFileSync(tail, 'all checks passed\n');
  const env = {
    ...process.env,
    PYTHONPATH: moduleDir,
    ORCH_LOCAL_RUNTIME: root,
    ORCH_STATE_DIR: root,
    ORCH_FEEDBACK_DB: path.join(root, 'brain.db'),
    ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
    HANDOFF_DIR: root,
    HANDOFF_PACING_LOG: log,
  };
  function run(args, command = python, expectedStatus = 0) {
    // File capture works on hosts that disallow child-process pipes.
    const output = path.join(root, 'stdout');
    const errors = path.join(root, 'stderr');
    const stdout = fs.openSync(output, 'w');
    const stderr = fs.openSync(errors, 'w');
    try {
      const result = spawnSync(command, args, {
        cwd: root, env, stdio: ['ignore', stdout, stderr], timeout: 30000,
      });
      assert.ifError(result.error);
      assert.equal(result.status, expectedStatus, fs.readFileSync(errors, 'utf8'));
      return fs.readFileSync(output, 'utf8').trim();
    } finally {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
    }
  }
  function cli(args) {
    return run([path.join(moduleDir, 'rate_incidents.py'), ...args]);
  }
  return {
    root, log, tail,
    report(end, start = deployment) {
      return reportWindow({ db: path.join(root, 'brain.db'), start, end });
    },
    reportCli(end, rawLogs = false, expectedStatus = 0) {
      return JSON.parse(run([
        path.join(repo, 'scripts', 'report_relay_join_window.js'),
        '--db', path.join(root, 'brain.db'), '--start', String(deployment), '--end', String(end),
        ...(rawLogs ? ['--pacing-log', log, '--exit-log', path.join(root, 'relay-exits.log')] : []),
      ], process.execPath, expectedStatus));
    },
    append(id, ts) {
      assert.equal(cli([
        'append-relay-pacing-decision', '--source', 'codex', '--lane', 'opener',
        '--candidates', 'claude_code,cursor', '--chosen', 'claude_code',
        '--weights', 'claude_code:1,cursor:1', '--states', 'claude_code:ok,cursor:ok',
        '--fallback', 'no', '--decision-id', id, '--ts', String(ts),
      ]), id);
    },
    exit(id, ts, status = 0) {
      const args = [
        'record-lane-round', '--agent', 'claude', '--surface', 'handoff-relay',
        '--lane', 'opener', '--exit', String(status), '--output-file', tail,
        '--ts', String(ts),
      ];
      if (id !== null) args.push('--relay-decision-id', id);
      return JSON.parse(cli(args));
    },
    snapshot(end) {
      return JSON.parse(run(['-c', `
import json
import sqlite3
from pathlib import Path
import feedback
import value_chain_monitor as monitor

with sqlite3.connect(Path(feedback.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True) as conn:
    conn.row_factory = sqlite3.Row
    rounds = [dict(r) for r in conn.execute(
        "SELECT decision_id,ts,causal_context_json FROM routing_decisions_v2 "
        "WHERE record_kind='lane_round' ORDER BY ts,decision_id")]
    joins = [dict(r) for r in conn.execute(
        "SELECT ie.source_run_id,ie.target_run_id,ie.accepted,"
        "source.created_ts AS decision_ts,target.created_ts AS exit_ts,"
        "json_extract(source.payload_json,'$.relay_pacing.decision_id') AS raw_id "
        "FROM influence_edges ie "
        "JOIN completion_events source ON source.event_id=ie.source_event_id "
        "JOIN completion_events target ON target.event_id=ie.target_event_id "
        "WHERE ie.capability_id=? AND ie.capability_version_id=? ORDER BY decision_ts",
        (feedback.RELAY_PACING_CAPABILITY, feedback.RELAY_PACING_VERSION))]
brain = monitor._brain(feedback.DB_PATH, ${deployment}, ${end})
assert not brain['errors'], brain['errors']
counts = monitor.outcome_counts(feedback.RELAY_PACING_CAPABILITY,
    monitor.own_edges_of(brain['edges'], feedback.RELAY_PACING_CAPABILITY))
print(json.dumps({'rounds': rounds, 'joins': joins, 'counts': counts}))
`]));
    },
  };
}

function receipt(w, id, ts, { status = 0, agent = 'claude', lane = 'opener' } = {}) {
  const stamp = new Date(ts * 1000).toISOString().replace('.000Z', 'Z');
  fs.appendFileSync(path.join(w.root, 'relay-exits.log'),
    `=== ${stamp} RELAY exit=${status} agent=${agent} lane=${lane} duration_s=35 decision_id=${id} ===\n`);
}

function rawReport(w, end) {
  return reportWindow({ db: path.join(w.root, 'brain.db'), start: deployment, end,
    pacingLog: w.log, exitLog: path.join(w.root, 'relay-exits.log') });
}

test('a pre-deployment decision exiting later does not satisfy the deployment window', (t) => {
  const w = world(t);
  w.append('before-deployment', deployment - 60);
  const old = w.exit('before-deployment', deployment + 10);
  assert.equal(old.relay_pacing.joined, 1);
  w.append('after-deployment', deployment + 20);

  // A positive cumulative counter and an exit inside the window are insufficient.
  const pending = w.snapshot(deployment + 30);
  assert.equal(pending.joins.length, 1);
  assert.equal(pending.counts.acted_on, 0);
  assert.equal(pending.counts.outcome, 0);

  const fresh = w.exit('after-deployment', deployment + 40);
  assert.equal(fresh.relay_pacing.joined, 2);
  const completed = w.snapshot(deployment + 40);
  assert.equal(completed.counts.acted_on, 1);
  assert.equal(completed.counts.outcome, 1);
  assert.deepEqual(completed.joins.map((j) => [
    j.raw_id, j.source_run_id, j.target_run_id, j.decision_ts, j.exit_ts, j.accepted,
  ]), [
    ['before-deployment', 'relay-pacing:id:before-deployment', old.decision_id,
      deployment - 60, deployment + 10, 1],
    ['after-deployment', 'relay-pacing:id:after-deployment', fresh.decision_id,
      deployment + 20, deployment + 40, 1],
  ]);
  assert.equal(JSON.parse(completed.rounds[1].causal_context_json).relay_decision_id,
    'after-deployment');
});

test('an importer failure retains the explicit round and provider incident for recovery', (t) => {
  const w = world(t);
  w.append('recoverable-decision', deployment + 10);
  const bytes = fs.readFileSync(w.log);
  fs.unlinkSync(w.log);
  fs.mkdirSync(w.log); // Demonstrated read error without replacing the persistence path.
  fs.writeFileSync(w.tail, 'HTTP 429 Too Many Requests\n');
  const failed = w.exit('recoverable-decision', deployment + 20, 1);
  assert.equal(failed.relay_pacing.status, 'unavailable');
  assert.equal(failed.error_class, 'http_429');
  assert.ok(failed.incident_id);
  assert.equal(failed.shed, true);
  const pending = w.snapshot(deployment + 20);
  assert.equal(pending.rounds.length, 1);
  assert.equal(pending.rounds[0].decision_id, failed.decision_id);
  assert.equal(JSON.parse(pending.rounds[0].causal_context_json).relay_decision_id,
    'recoverable-decision');
  assert.equal(pending.joins.length, 0);

  fs.rmdirSync(w.log);
  fs.writeFileSync(w.log, bytes);
  // Replaying is safe only in this isolated fixture; never replay a production hook.
  const recovered = w.exit('recoverable-decision', deployment + 20, 1);
  assert.equal(recovered.decision_id, failed.decision_id);
  assert.equal(recovered.incident_id, failed.incident_id);
  assert.equal(recovered.relay_pacing.joined, 1);
  const settled = w.snapshot(deployment + 20);
  assert.equal(settled.rounds.length, 1);
  assert.equal(settled.joins.length, 1);
  assert.equal(settled.joins[0].target_run_id, failed.decision_id);
  assert.equal(settled.counts.acted_on, 1);
  assert.equal(settled.counts.outcome, 1);
  assert.equal(settled.counts.outcome_success, 0);
  const incidents = fs.readFileSync(path.join(w.root, 'rate-limit-incidents.ndjson'), 'utf8')
    .trim().split('\n').map(JSON.parse);
  assert.equal(incidents.length, 1);
  assert.equal(incidents[0].run_id, failed.decision_id);
});

test('an explicit exit remains a boundary for later legacy adjacency', (t) => {
  const w = world(t);
  w.append('legacy-fixture', deployment + 10);
  fs.writeFileSync(w.log, fs.readFileSync(w.log, 'utf8')
    .replace('decision_id=legacy-fixture ', ''));
  w.append('explicit-decision', deployment + 20);
  const explicit = w.exit('explicit-decision', deployment + 30);
  assert.equal(explicit.relay_pacing.joined, 1);
  const legacy = w.exit(null, deployment + 40);
  assert.equal(legacy.relay_pacing.joined, 1);
  const snapshot = w.snapshot(deployment + 40);
  assert.equal(snapshot.rounds.length, 2);
  assert.equal(snapshot.joins.length, 1);
  assert.equal(snapshot.joins[0].target_run_id, explicit.decision_id);
  assert.equal(snapshot.counts.acted_on, 1);
  assert.equal(snapshot.counts.outcome, 1);
});

test('interleaved decisions retain exact attribution while an unfinished decision stays pending', (t) => {
  const w = world(t);
  w.append('first-decision', deployment + 10);
  w.append('second-decision', deployment + 20);
  w.append('unfinished-decision', deployment + 30);

  // Completion order alone cannot identify which decision earned the join.
  const second = w.exit('second-decision', deployment + 40);
  assert.equal(second.relay_pacing.decisions, 3);
  assert.equal(second.relay_pacing.joined, 1);
  const partial = w.snapshot(deployment + 40);
  assert.equal(partial.counts.acted_on, 1);
  assert.equal(partial.counts.outcome, 1);
  assert.deepEqual(partial.joins.map((j) => [j.raw_id, j.target_run_id]), [
    ['second-decision', second.decision_id],
  ]);

  const first = w.exit('first-decision', deployment + 50);
  assert.equal(first.relay_pacing.joined, 2);
  const complete = w.snapshot(deployment + 50);
  assert.equal(complete.counts.acted_on, 2);
  assert.equal(complete.counts.outcome, 2);
  assert.deepEqual(complete.joins.map((j) => [
    j.raw_id, j.source_run_id, j.target_run_id, j.decision_ts, j.exit_ts, j.accepted,
  ]), [
    ['first-decision', 'relay-pacing:id:first-decision', first.decision_id,
      deployment + 10, deployment + 50, 1],
    ['second-decision', 'relay-pacing:id:second-decision', second.decision_id,
      deployment + 20, deployment + 40, 1],
  ]);
  for (const join of complete.joins) {
    const round = complete.rounds.find((r) => r.decision_id === join.target_run_id);
    assert.ok(round);
    assert.equal(round.ts, join.exit_ts);
    assert.equal(JSON.parse(round.causal_context_json).relay_decision_id, join.raw_id);
  }
  assert.equal(complete.rounds.length, 2);
});

test('a completed exit after the deadline cannot establish an outcome before the deadline', (t) => {
  const w = world(t);
  w.append('late-completion', deadline - 60);
  const late = w.exit('late-completion', deadline + 1);
  assert.equal(late.relay_pacing.joined, 1);

  // Read the earlier window after persistence: even a backdated edge timestamp
  // and a positive cumulative joined counter must not pull the later exit into it.
  const timely = w.snapshot(deadline);
  assert.equal(timely.joins.length, 1);
  assert.equal(timely.counts.acted_on, 1);
  assert.equal(timely.counts.outcome, 0);
  assert.equal(timely.counts.outcome_success, 0);
  assert.deepEqual(timely.joins.map((j) => [
    j.raw_id, j.target_run_id, j.decision_ts, j.exit_ts, j.accepted,
  ]), [['late-completion', late.decision_id, deadline - 60, deadline + 1, 1]]);

  const recovery = w.snapshot(deadline + 1);
  assert.equal(recovery.counts.acted_on, 1);
  assert.equal(recovery.counts.outcome, 1);
  assert.equal(recovery.counts.outcome_success, 1);
  assert.deepEqual(recovery.joins, timely.joins);

  // Observing different windows must not import, replay, or rewrite evidence.
  const before = ['brain.db', 'relay-pacing.log'].map((file) =>
    fs.readFileSync(path.join(w.root, file)));
  assert.deepEqual(w.snapshot(deadline), timely);
  const after = ['brain.db', 'relay-pacing.log'].map((file) =>
    fs.readFileSync(path.join(w.root, file)));
  assert.deepEqual(after, before);
});

test('the deployment boundary excludes an earlier decision despite both exits being in-window', (t) => {
  const w = world(t);
  w.append('one-second-before', deployment - 1);
  const older = w.exit('one-second-before', deployment + 10);
  w.append('at-deployment', deployment);
  const current = w.exit('at-deployment', deployment + 20);
  assert.equal(current.relay_pacing.joined, 2);

  const snapshot = w.snapshot(deployment + 20);
  assert.equal(snapshot.joins.length, 2);
  assert.equal(snapshot.counts.acted_on, 1);
  assert.equal(snapshot.counts.outcome, 1);
  assert.deepEqual(snapshot.joins.map((j) => [j.raw_id, j.target_run_id, j.decision_ts]), [
    ['one-second-before', older.decision_id, deployment - 1],
    ['at-deployment', current.decision_id, deployment],
  ]);
});

test('the report separates cumulative bindings from completed joins in the selected window', (t) => {
  const w = world(t);
  w.append('old-binding', deployment - 1);
  w.exit('old-binding', deployment + 10);
  w.append('timely-binding', deployment + 20);
  const timely = w.exit('timely-binding', deployment + 30);
  w.append('late-binding', deployment + 40);
  const late = w.exit('late-binding', deadline + 1);

  const before = fs.readFileSync(path.join(w.root, 'brain.db'));
  const report = w.reportCli(deadline);
  assert.equal(report.status, 'available');
  assert.equal(report.cumulative_accepted_bindings_present, 3);
  assert.equal(report.window_decisions, 2);
  assert.equal(report.matching_completed_exit_decisions, 1);
  assert.equal(report.accepted_window_joins, 1);
  assert.equal(report.missing_join_decisions, 0);
  assert.deepEqual(report.decisions.map((d) => [
    d.raw_decision_id, d.decision_ts, d.status,
    d.completed_rounds.map((r) => [r.decision_id, r.ts]), d.accepted_edge_ids.length,
  ]), [
    ['timely-binding', deployment + 20, 'accepted_join', [[timely.decision_id, deployment + 30]], 1],
    ['late-binding', deployment + 40, 'no_completed_exit', [], 0],
  ]);
  const recovery = w.report(deadline + 1);
  assert.equal(recovery.accepted_window_joins, 2);
  assert.equal(recovery.decisions[1].completed_rounds[0].decision_id, late.decision_id);
  assert.deepEqual(w.report(deadline), report);
  assert.deepEqual(fs.readFileSync(path.join(w.root, 'brain.db')), before);
});

test('positive window totals do not conceal a matching completed exit with no accepted join', (t) => {
  const w = world(t);
  w.append('joined-decision', deployment + 10);
  w.append('missing-join', deployment + 20);
  // This import persists both source decisions, but only one has completed.
  w.exit('joined-decision', deployment + 30);
  fs.unlinkSync(w.log);
  fs.mkdirSync(w.log);
  const missing = w.exit('missing-join', deployment + 40);
  assert.equal(missing.relay_pacing.status, 'unavailable');
  const counts = w.snapshot(deployment + 40).counts;
  assert.equal(counts.acted_on, 1);
  assert.equal(counts.outcome, 1);

  const before = fs.readFileSync(path.join(w.root, 'brain.db'));
  const report = w.report(deployment + 40);
  assert.equal(report.status, 'available');
  assert.equal(report.cumulative_accepted_bindings_present, 1);
  assert.equal(report.matching_completed_exit_decisions, 2);
  assert.equal(report.accepted_window_joins, 1);
  assert.equal(report.missing_join_decisions, 1);
  const decision = report.decisions.find((d) => d.raw_decision_id === 'missing-join');
  assert.equal(decision.status, 'missing_accepted_join');
  assert.equal(decision.decision_ts, deployment + 20);
  assert.equal(decision.completed_rounds[0].decision_id, missing.decision_id);
  assert.equal(decision.completed_rounds[0].ts, deployment + 40);
  assert.deepEqual(decision.accepted_edge_ids, []);
  assert.deepEqual(fs.readFileSync(path.join(w.root, 'brain.db')), before);
});

test('ambiguous persisted exits cannot be reported as an attributable accepted join', (t) => {
  const w = world(t);
  w.append('duplicate-exit', deployment + 10);
  const first = w.exit('duplicate-exit', deployment + 20);
  const second = w.exit('duplicate-exit', deployment + 30);
  const report = w.report(deployment + 30);
  assert.equal(report.cumulative_accepted_bindings_present, 1);
  assert.equal(report.matching_completed_exit_decisions, 0);
  assert.equal(report.accepted_window_joins, 0);
  assert.equal(report.decisions[0].status, 'ambiguous_completed_exits');
  assert.deepEqual(report.decisions[0].completed_rounds.map((r) => r.decision_id),
    [first.decision_id, second.decision_id]);
  assert.deepEqual(report.decisions[0].accepted_edge_ids, []);
});

test('unavailable Brain reports unknown counts without creating a database', (t) => {
  const w = world(t);
  const report = w.report(deadline);
  assert.equal(report.status, 'unavailable');
  assert.equal(report.cumulative_accepted_bindings_present, null);
  assert.equal(report.matching_completed_exit_decisions, null);
  assert.equal(report.accepted_window_joins, null);
  assert.equal(report.decisions, null);
  assert.equal(report.errors.length, 1);
  assert.equal(fs.existsSync(path.join(w.root, 'brain.db')), false);
  assert.throws(() => w.report(deployment - 1), /ordered/);
});

test('an accepted flag cannot substitute for matching round facts and relay outcome evidence', (t) => {
  const w = world(t);
  w.append('exact-outcome', deployment + 10);
  const completed = w.exit('exact-outcome', deployment + 20);
  assert.equal(w.report(deployment + 20).accepted_window_joins, 1);
  const db = new DatabaseSync(path.join(w.root, 'brain.db'));
  try {
    // Corrupt only this private fixture, retaining the accepted edge unchanged.
    db.prepare("UPDATE completion_events SET producer='foreign' WHERE run_id=?")
      .run(completed.decision_id);
    const foreign = w.report(deployment + 20);
    assert.equal(foreign.cumulative_accepted_bindings_present, 1);
    assert.equal(foreign.matching_completed_exit_decisions, 1);
    assert.equal(foreign.accepted_window_joins, 0);
    assert.equal(foreign.missing_join_decisions, 1);

    db.prepare("UPDATE routing_decisions_v2 SET agent='cursor' WHERE decision_id=?")
      .run(completed.decision_id);
    const mismatch = w.report(deployment + 20);
    assert.equal(mismatch.matching_completed_exit_decisions, 0);
    assert.equal(mismatch.accepted_window_joins, 0);
    assert.equal(mismatch.decisions[0].status, 'completed_exit_mismatch');
  } finally {
    db.close();
  }
});

test('raw receipts expose a completed decision that was never imported into Brain', (t) => {
  const w = world(t);
  w.append('unimported-decision', deployment + 10);
  const original = fs.readFileSync(w.log);
  fs.unlinkSync(w.log);
  fs.mkdirSync(w.log);
  const completed = w.exit('unimported-decision', deployment + 20);
  assert.equal(completed.relay_pacing.status, 'unavailable');
  // Restore only the fixture log. Do not run an importer or replay the fixture hook.
  fs.rmdirSync(w.log);
  fs.writeFileSync(w.log, original);
  receipt(w, 'unimported-decision', deployment + 20);

  const files = [path.join(w.root, 'brain.db'), w.log, path.join(w.root, 'relay-exits.log')];
  const before = files.map((file) => fs.readFileSync(file));
  const report = rawReport(w, deployment + 20);
  assert.deepEqual(w.reportCli(deployment + 20, true), report);
  assert.equal(report.window_decisions, 0); // Persisted-only population misses this decision.
  const audit = report.raw_log_correlation;
  assert.equal(audit.status, 'available');
  assert.equal(audit.window_decisions, 1);
  assert.equal(audit.matching_completed_exit_decisions, 1);
  assert.equal(audit.accepted_window_joins, 0);
  assert.equal(audit.unattributed_completed_exit_decisions, 1);
  assert.equal(audit.decisions[0].status, 'missing_persisted_decision');
  assert.equal(audit.decisions[0].raw_decision_id, 'unimported-decision');
  assert.equal(audit.decisions[0].decision_ts, deployment + 10);
  assert.equal(audit.decisions[0].persisted_decision, null);
  assert.equal(audit.decisions[0].persisted_rounds[0].decision_id, completed.decision_id);
  assert.equal(audit.decisions[0].persisted_rounds[0].ts, deployment + 20);
  assert.equal(audit.decisions[0].raw_completed_exits[0].ts, deployment + 20);
  assert.deepEqual(rawReport(w, deployment + 20), report);
  assert.deepEqual(files.map((file) => fs.readFileSync(file)), before);
});

test('raw correlation retains exact receipts and separates timely joins from later recovery', (t) => {
  const w = world(t);
  w.append('old-raw-decision', deployment - 1);
  w.exit('old-raw-decision', deployment + 1);
  receipt(w, 'old-raw-decision', deployment + 1);
  w.append('timely-raw-decision', deployment);
  const timely = w.exit('timely-raw-decision', deployment + 10, 1);
  receipt(w, 'timely-raw-decision', deployment + 10, { status: 1 });
  w.append('late-raw-decision', deadline - 1);
  w.exit('late-raw-decision', deadline + 1);
  receipt(w, 'late-raw-decision', deadline + 1);

  const report = rawReport(w, deadline);
  assert.equal(report.cumulative_accepted_bindings_present, 3);
  const audit = report.raw_log_correlation;
  assert.equal(audit.status, 'available');
  assert.equal(audit.window_decisions, 2);
  assert.equal(audit.matching_completed_exit_decisions, 1);
  assert.equal(audit.accepted_window_joins, 1);
  assert.equal(audit.unattributed_completed_exit_decisions, 0);
  assert.deepEqual(audit.decisions.map((d) => [d.raw_decision_id, d.status]), [
    ['timely-raw-decision', 'accepted_join'], ['late-raw-decision', 'no_completed_exit_receipt'],
  ]);
  const accepted = audit.decisions[0];
  assert.match(accepted.raw_pacing_lines[0].text, /decision_id=timely-raw-decision /);
  assert.equal(accepted.raw_pacing_lines[0].line_number, 2);
  assert.match(accepted.raw_completed_exits[0].text, /decision_id=timely-raw-decision ===$/);
  assert.equal(accepted.persisted_decision.completed_rounds[0].decision_id, timely.decision_id);
  assert.equal(accepted.accepted_edge_ids.length, 1);
  assert.equal(audit.files[w.log].sha256.length, 64);
  assert.equal(audit.files[w.log].bytes, fs.statSync(w.log).size);

  const recovery = rawReport(w, deadline + 1).raw_log_correlation;
  assert.equal(recovery.accepted_window_joins, 2);
  assert.equal(recovery.decisions[1].raw_completed_exits[0].ts, deadline + 1);
});

test('raw correlation requires receipt facts to agree with the persisted round', (t) => {
  const w = world(t);
  w.append('receipt-mismatch', deployment + 10);
  w.exit('receipt-mismatch', deployment + 20);
  const exitLog = path.join(w.root, 'relay-exits.log');
  for (const facts of [
    { status: 1 }, { agent: 'cursor' }, { lane: 'closer' }, { ts: deployment + 21 },
  ]) {
    fs.writeFileSync(exitLog, '');
    receipt(w, 'receipt-mismatch', facts.ts || deployment + 20, facts);
    const report = rawReport(w, deployment + 30);
    assert.equal(report.accepted_window_joins, 1);
    assert.equal(report.raw_log_correlation.accepted_window_joins, 0);
    assert.match(report.raw_log_correlation.decisions[0].status, /mismatch/);
    assert.deepEqual(report.raw_log_correlation.decisions[0].accepted_edge_ids, []);
  }
  // An accepted Brain edge also cannot choose between conflicting raw exit facts.
  receipt(w, 'receipt-mismatch', deployment + 20);
  const ambiguous = rawReport(w, deployment + 30).raw_log_correlation;
  assert.equal(ambiguous.decisions[0].status, 'ambiguous_completed_exit_receipts');
  assert.equal(ambiguous.accepted_window_joins, 0);
});

test('conflicting raw pacing facts cannot earn an attributable join', (t) => {
  const w = world(t);
  w.append('conflicting-raw', deployment + 10);
  w.exit('conflicting-raw', deployment + 20);
  receipt(w, 'conflicting-raw', deployment + 20);
  const original = fs.readFileSync(w.log, 'utf8');
  fs.writeFileSync(w.log, original.replace('weights=claude_code:1', 'weights=claude_code:2'));
  const changed = rawReport(w, deployment + 20).raw_log_correlation;
  assert.equal(changed.decisions[0].status, 'persisted_decision_mismatch');
  assert.equal(changed.accepted_window_joins, 0);
  fs.writeFileSync(w.log, original);
  fs.appendFileSync(w.log, original); // An exact repeated line is harmless and retained.
  assert.equal(rawReport(w, deployment + 20).raw_log_correlation.accepted_window_joins, 1);
  fs.appendFileSync(w.log, original.replace('lane=opener', 'lane=closer'));
  const audit = rawReport(w, deployment + 20).raw_log_correlation;
  assert.equal(audit.decisions[0].status, 'conflicting_raw_decisions');
  assert.equal(audit.decisions[0].raw_pacing_lines.length, 3);
  assert.equal(audit.accepted_window_joins, 0);
});

test('missing or malformed raw evidence stays unavailable or incomplete', (t) => {
  const w = world(t);
  w.append('raw-input-errors', deployment + 10);
  w.exit('raw-input-errors', deployment + 20);
  const missing = rawReport(w, deployment + 20).raw_log_correlation;
  assert.equal(missing.status, 'unavailable');
  assert.equal(missing.accepted_window_joins, null);
  assert.equal(missing.decisions, null);
  assert.deepEqual(w.reportCli(deployment + 20, true, 1).raw_log_correlation, missing);
  assert.equal(fs.existsSync(path.join(w.root, 'relay-exits.log')), false);
  assert.throws(() => reportWindow({ db: path.join(w.root, 'brain.db'), start: deployment,
    end: deadline, pacingLog: w.log }), /required together/);
  receipt(w, 'raw-input-errors', deployment + 20);
  fs.appendFileSync(w.log, '2026-11-03T08:17:00Z decision_id=truncated\n');
  const incomplete = rawReport(w, deployment + 60).raw_log_correlation;
  assert.equal(incomplete.status, 'incomplete');
  assert.equal(incomplete.accepted_window_joins, 1); // Only the valid subset is counted.
  assert.match(incomplete.errors[0], /:2: missing pacing field/);
  assert.deepEqual(w.reportCli(deployment + 60, true, 1).raw_log_correlation, incomplete);
});
