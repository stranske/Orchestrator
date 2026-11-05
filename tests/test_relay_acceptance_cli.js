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
  function run(args, command = python) {
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
      assert.equal(result.status, 0, fs.readFileSync(errors, 'utf8'));
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
    report(end, start = deployment, pacingLog, exitLog) {
      return reportWindow({ db: path.join(root, 'brain.db'), start, end, pacingLog, exitLog });
    },
    reportCli(end, pacingLog, exitLog) {
      return JSON.parse(run([
        path.join(repo, 'scripts', 'report_relay_join_window.js'),
        '--db', path.join(root, 'brain.db'), '--start', String(deployment), '--end', String(end),
        ...(pacingLog ? ['--pacing-log', pacingLog] : []),
        ...(exitLog ? ['--exit-log', exitLog] : []),
      ], process.execPath));
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

function exitLine(id, ts, status = 0, agent = 'claude', lane = 'opener') {
  const stamp = new Date(ts * 1000).toISOString().replace('.000Z', 'Z');
  return `=== ${stamp} RELAY exit=${status} agent=${agent} lane=${lane} duration_s=35 decision_id=${id} ===\n`;
}

test('raw exits expose completed decisions whose rounds never reached Brain', (t) => {
  const w = world(t);
  w.append('joined', deployment);
  const joined = w.exit('joined', deployment + 10);
  w.append('Missing.+:[1]', deployment + 20);
  const exits = path.join(w.root, 'relay-exits.log');
  fs.writeFileSync(exits, exitLine('joined', deployment + 10)
    + exitLine('Missing.+:[1]', deployment + 30, 78));
  const files = ['brain.db', 'relay-pacing.log', 'relay-exits.log'];
  const before = files.map((f) => fs.readFileSync(path.join(w.root, f)));
  const report = w.reportCli(deployment + 30, w.log, exits);
  const raw = report.raw_reconciliation;
  assert.equal(raw.matching_completed_exit_decisions, 2);
  assert.equal(raw.accepted_window_joins, 1);
  assert.equal(raw.unaccounted_completed_exit_decisions, 1);
  assert.equal(raw.decisions[0].exit_reconciliation_status, 'matched_persisted_round');
  assert.equal(raw.decisions[0].completed_rounds[0].decision_id, joined.decision_id);
  const missing = raw.decisions[1];
  assert.equal(missing.status, 'missing_persisted_decision');
  assert.equal(missing.exit_reconciliation_status, 'missing_persisted_round');
  assert.equal(missing.raw_exit_records[0].exit_ts, deployment + 30);
  assert.equal(missing.raw_exit_records[0].exit_status, 78);
  assert.equal(missing.raw_exit_records[0].raw_line, exitLine('Missing.+:[1]', deployment + 30, 78).trim());
  assert.deepEqual(missing.completed_rounds, []);
  assert.deepEqual(missing.accepted_edge_ids, []);
  assert.equal(raw.exit_log_sha256, require('node:crypto').createHash('sha256')
    .update(before[2]).digest('hex'));
  assert.deepEqual(w.report(deployment + 30, deployment, w.log, exits), report);
  assert.deepEqual(files.map((f) => fs.readFileSync(path.join(w.root, f))), before);
});

test('accepted persisted joins require unique raw exits with matching timestamp and facts', (t) => {
  const w = world(t);
  w.append('exact', deployment + 10);
  w.exit('exact', deployment + 20);
  const exits = path.join(w.root, 'relay-exits.log');
  const original = exitLine('exact', deployment + 20);
  for (const [line, reason, eligible] of [
    ['', 'no_raw_exit', false],
    [original + original, 'ambiguous_raw_exits', true],
    [exitLine('exact', deployment + 9), 'raw_exit_mismatch', false],
    [exitLine('exact', deployment + 20, 0, 'cursor'), 'raw_exit_mismatch', false],
    [exitLine('exact', deployment + 20, 0, 'claude', 'closer'), 'raw_exit_mismatch', false],
    [exitLine('exact', deployment + 21), 'persisted_exit_mismatch', true],
    [exitLine('exact', deployment + 20, 78), 'persisted_exit_mismatch', true],
  ]) {
    fs.writeFileSync(exits, line);
    const report = w.report(deployment + 30, deployment, w.log, exits);
    assert.equal(report.accepted_window_joins, 1);
    const raw = report.raw_reconciliation;
    assert.equal(raw.accepted_window_joins, 0, reason);
    assert.equal(raw.decisions[0].status, reason);
    assert.equal(raw.decisions[0].exit_reconciliation_status, reason);
    assert.equal(raw.unaccounted_completed_exit_decisions, Number(eligible), reason);
    assert.deepEqual(raw.decisions[0].accepted_edge_ids, []);
  }
  fs.writeFileSync(exits, original.replace('agent=claude ', 'agent=claude_code '));
  assert.equal(w.report(deployment + 30, deployment, w.log, exits)
    .raw_reconciliation.accepted_window_joins, 1);
  w.exit('exact', deployment + 25);
  const ambiguous = w.report(deployment + 30, deployment, w.log, exits).raw_reconciliation;
  assert.equal(ambiguous.accepted_window_joins, 0);
  assert.equal(ambiguous.unaccounted_completed_exit_decisions, 1);
  assert.equal(ambiguous.decisions[0].exit_reconciliation_status, 'ambiguous_completed_exits');
});

test('ambiguous exits remain unaccounted despite positive accepted join totals', (t) => {
  const w = world(t);
  w.append('joined', deployment);
  w.exit('joined', deployment + 5);
  w.append('ambiguous', deployment + 10);
  const first = w.exit('ambiguous', deployment + 20);
  const second = w.exit('ambiguous', deployment + 21);
  const exits = path.join(w.root, 'relay-exits.log');
  const good = exitLine('joined', deployment + 5);
  const original = exitLine('ambiguous', deployment + 20);
  const files = ['brain.db', 'relay-pacing.log'];
  const before = files.map((f) => fs.readFileSync(path.join(w.root, f)));
  for (const trailers of [
    null, // Persisted-round comparison must also retain the ambiguous obligation.
    original + original, // Even identical duplicate trailers prevent attribution.
    original + exitLine('ambiguous', deployment + 21),
    exitLine('ambiguous', deployment + 20, 0, 'cursor') + original,
  ]) {
    if (trailers !== null) fs.writeFileSync(exits, good + trailers);
    const raw = w.report(deployment + 21, deployment, w.log,
      trailers === null ? undefined : exits).raw_reconciliation;
    assert.equal(raw.window_decisions, 2);
    assert.equal(raw.matching_completed_exit_decisions, 2);
    assert.equal(raw.accepted_window_joins, 1);
    assert.equal(raw.unaccounted_completed_exit_decisions, 1);
    const ambiguous = raw.decisions[1];
    assert.equal(ambiguous.matching_completed_exit, true);
    assert.equal(ambiguous.status, 'ambiguous_completed_exits');
    if (trailers !== null) {
      assert.equal(ambiguous.exit_reconciliation_status, 'ambiguous_raw_exits');
      assert.equal(ambiguous.raw_exit_records.length, 2);
    }
    assert.deepEqual(ambiguous.completed_rounds.map((r) => r.decision_id),
      [first.decision_id, second.decision_id]);
    assert.deepEqual(ambiguous.accepted_edge_ids, []);
  }
  // Multiple trailers without a single matching agent are not completed-exit evidence.
  fs.writeFileSync(exits, good + exitLine('ambiguous', deployment + 20, 0, 'cursor')
    + exitLine('ambiguous', deployment + 21, 0, 'cursor'));
  const mismatch = w.report(deployment + 21, deployment, w.log, exits).raw_reconciliation;
  assert.equal(mismatch.matching_completed_exit_decisions, 1);
  assert.equal(mismatch.unaccounted_completed_exit_decisions, 0);
  assert.equal(mismatch.decisions[1].matching_completed_exit, false);
  assert.deepEqual(files.map((f) => fs.readFileSync(path.join(w.root, f))), before);
});

test('raw exit reconciliation preserves exact IDs and window boundaries and excludes invalid lines', (t) => {
  const w = world(t);
  w.append('Decision.+:[1]', deployment);
  w.exit('Decision.+:[1]', deployment);
  w.append('decision.+:[1]', deployment + 10);
  w.exit('decision.+:[1]', deployment + 20);
  const exits = path.join(w.root, 'relay-exits.log');
  const boundary = exitLine('Decision.+:[1]', deployment);
  const excluded = [
    boundary.replace('2026-11-03', '2026-02-30'),
    boundary.replace('exit=0', 'exit=unknown'),
    boundary.replace('lane=opener', 'lane=unknown'),
    boundary.replace('agent=claude', 'agent=unknown'),
    boundary.replace('decision_id=Decision.+:[1]', 'decision_id='),
    boundary.replace('duration_s=35', 'duration_s=-1'),
    boundary.replace('exit=0', 'exit=0 exit=78'),
    'unfinished trailer\n',
  ];
  fs.writeFileSync(exits, boundary + exitLine('earlier', deployment - 1)
    + exitLine('decision.+:[1]', deployment + 21)
    + exitLine('Decision.+:[1]-suffix', deployment + 20) + excluded.join(''));
  const raw = w.report(deployment + 20, deployment, w.log, exits).raw_reconciliation;
  assert.equal(raw.accepted_window_joins, 1);
  assert.equal(raw.excluded_exit_lines, excluded.length);
  assert.equal(raw.decisions[0].raw_exit_records[0].exit_ts, deployment);
  assert.equal(raw.decisions[1].exit_reconciliation_status, 'no_raw_exit');
  assert.deepEqual(raw.unmatched_exit_records.map((r) => r.raw_decision_id),
    ['Decision.+:[1]-suffix']);
});

test('requesting raw exits requires a retained pacing log and fails closed on unreadable exits', (t) => {
  const w = world(t);
  w.append('joined', deployment);
  w.exit('joined', deployment + 10);
  const absent = path.join(w.root, 'absent-exits.log');
  assert.throws(() => w.report(deployment + 10, deployment, undefined, absent), /pacing/);
  const report = w.report(deployment + 10, deployment, w.log, absent);
  assert.equal(report.status, 'unavailable');
  assert.equal(report.accepted_window_joins, null);
  assert.equal(report.raw_reconciliation, null);
  assert.equal(fs.existsSync(absent), false);
});

test('raw reconciliation exposes an unpersisted decision despite positive persisted joins', (t) => {
  const w = world(t);
  w.append('joined', deployment + 10);
  const joined = w.exit('joined', deployment + 20);
  w.append('Missing.+:[1]', deployment + 30);
  const missing = w.exit('Missing.+:[1]', deployment + 40);
  w.append('pending', deployment + 50);
  const db = new DatabaseSync(path.join(w.root, 'brain.db'));
  try {
    db.prepare("UPDATE completion_events SET producer='foreign' WHERE run_id=?")
      .run('relay-pacing:id:Missing.+:[1]');
  } finally {
    db.close();
  }
  const before = ['brain.db', 'relay-pacing.log'].map((f) => fs.readFileSync(path.join(w.root, f)));
  const report = w.reportCli(deployment + 60, w.log);
  assert.equal(report.status, 'available');
  assert.equal(report.accepted_window_joins, 1);
  const raw = report.raw_reconciliation;
  assert.equal(raw.window_decisions, 3);
  assert.equal(raw.matching_completed_exit_decisions, 2);
  assert.equal(raw.accepted_window_joins, 1);
  assert.equal(raw.unaccounted_completed_exit_decisions, 1);
  assert.deepEqual(raw.decisions.map((d) => [d.raw_decision_id, d.decision_ts,
    d.status, d.completed_rounds.map((r) => [r.decision_id, r.ts])]), [
    ['joined', deployment + 10, 'accepted_join', [[joined.decision_id, deployment + 20]]],
    ['Missing.+:[1]', deployment + 30, 'missing_persisted_decision',
      [[missing.decision_id, deployment + 40]]],
    ['pending', deployment + 50, 'missing_persisted_decision', []],
  ]);
  assert.deepEqual(raw.decisions[1].accepted_edge_ids, []);
  assert.equal(raw.pacing_log_sha256, require('node:crypto').createHash('sha256')
    .update(before[1]).digest('hex'));
  assert.deepEqual(w.report(deployment + 60, deployment, w.log), report);
  assert.deepEqual(['brain.db', 'relay-pacing.log'].map((f) =>
    fs.readFileSync(path.join(w.root, f))), before);
});

test('raw facts must agree with the persisted decision before an accepted join is credited', (t) => {
  const w = world(t);
  w.append('changed', deployment + 10);
  w.exit('changed', deployment + 20);
  const original = fs.readFileSync(w.log, 'utf8');
  for (const changed of [
    original.replace('lane=opener', 'lane=closer'),
    original.replace('chosen=claude_code', 'chosen=cursor'),
    original.replace(new Date((deployment + 10) * 1000).toISOString().replace('.000Z', 'Z'),
      new Date((deployment + 11) * 1000).toISOString().replace('.000Z', 'Z')),
  ]) {
    fs.writeFileSync(w.log, changed);
    const report = w.report(deployment + 20, deployment, w.log);
    assert.equal(report.accepted_window_joins, 1);
    assert.equal(report.raw_reconciliation.accepted_window_joins, 0);
    assert.equal(report.raw_reconciliation.decisions[0].status, 'persisted_decision_mismatch');
    assert.deepEqual(report.raw_reconciliation.decisions[0].accepted_edge_ids, []);
  }
});

test('raw reconciliation filters the window and rejects conflicting reuse of an exact ID', (t) => {
  const w = world(t);
  w.append('before', deployment - 1);
  w.append('boundary', deployment);
  w.exit('boundary', deployment + 10);
  w.append('at-end', deployment + 20);
  w.append('after', deployment + 21);
  const original = fs.readFileSync(w.log, 'utf8');
  const boundary = original.split('\n').find((line) => line.includes('decision_id=boundary '));
  fs.appendFileSync(w.log, `${boundary}\ninvalid trailing prose\n`);
  let raw = w.report(deployment + 20, deployment, w.log).raw_reconciliation;
  assert.deepEqual(raw.decisions.map((d) => d.raw_decision_id), ['boundary', 'at-end']);
  assert.equal(raw.accepted_window_joins, 1);
  fs.appendFileSync(w.log, `${boundary.replace('chosen=claude_code', 'chosen=cursor')}\n`);
  raw = w.report(deployment + 20, deployment, w.log).raw_reconciliation;
  assert.equal(raw.decisions[0].status, 'ambiguous_raw_decisions');
  assert.equal(raw.decisions[0].raw_records.length, 2);
  assert.equal(raw.accepted_window_joins, 0);
  assert.deepEqual(raw.decisions[0].accepted_edge_ids, []);
});

test('a requested unavailable pacing log fails closed without changing stored state', (t) => {
  const w = world(t);
  w.append('joined', deployment + 10);
  w.exit('joined', deployment + 20);
  const absent = path.join(w.root, 'absent.log');
  const report = w.report(deployment + 20, deployment, absent);
  assert.equal(report.status, 'unavailable');
  assert.equal(report.accepted_window_joins, null);
  assert.equal(report.raw_reconciliation, null);
  assert.equal(report.errors.length, 1);
  assert.equal(fs.existsSync(absent), false);
});

test('invalid and legacy pacing lines cannot enter the explicit raw reconciliation population', (t) => {
  const w = world(t);
  w.append('eligible', deployment);
  w.exit('eligible', deployment + 10);
  const line = fs.readFileSync(w.log, 'utf8').trim();
  const excluded = [
    line.replace(/2026-11-03/, '2026-02-30'),
    line.replace('source=codex', 'source=unknown'),
    line.replace('lane=opener', 'lane=unknown'),
    line.replace('candidates=claude_code,cursor', 'candidates=claude_code,unknown'),
    line.replace('chosen=claude_code', 'chosen=codex'),
    line.replace('fallback=no', 'fallback=maybe'),
    line.replace(/weights=\S+ /, ''),
    line.replace('decision_id=eligible', 'decision_id='),
    line.replace('decision_id=eligible ', ''),
    line.replace('decision_id=eligible', `decision_id=${'x'.repeat(178)}`),
    `${line} incomplete`,
  ];
  fs.appendFileSync(w.log, `${excluded.join('\n')}\n`);
  const raw = w.report(deployment + 10, deployment, w.log).raw_reconciliation;
  assert.equal(raw.window_decisions, 1);
  assert.equal(raw.accepted_window_joins, 1);
  assert.equal(raw.excluded_lines, excluded.length);
  assert.equal(raw.decisions[0].raw_decision_id, 'eligible');
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

test('relay outcome lane and chosen agent must agree with the attributed decision', async (t) => {
  for (const [field, value] of [['lane', 'closer'], ['chosen', 'cursor']]) {
    await t.test(field, (t) => {
      const w = world(t);
      w.append('outcome-facts', deployment + 10);
      const completed = w.exit('outcome-facts', deployment + 20);
      assert.equal(w.report(deployment + 20).accepted_window_joins, 1);
      const db = new DatabaseSync(path.join(w.root, 'brain.db'));
      try {
        const original = db.prepare('SELECT payload_json FROM completion_events WHERE run_id=?')
          .get(completed.decision_id).payload_json;
        // Keep IDs, timestamps, round facts and the accepted edge intact.
        db.prepare('UPDATE completion_events SET payload_json=json_set(payload_json,?,?) WHERE run_id=?')
          .run(`$.relay_pacing.${field}`, value, completed.decision_id);
        const report = w.report(deployment + 20);
        assert.equal(report.status, 'available');
        assert.equal(report.cumulative_accepted_bindings_present, 1);
        assert.equal(report.matching_completed_exit_decisions, 1);
        assert.equal(report.accepted_window_joins, 0);
        assert.equal(report.missing_join_decisions, 1);
        assert.equal(report.decisions[0].status, 'missing_accepted_join');
        assert.deepEqual(report.decisions[0].accepted_edge_ids, []);
        db.prepare('UPDATE completion_events SET payload_json=? WHERE run_id=?')
          .run(original, completed.decision_id);
        assert.equal(w.report(deployment + 20).accepted_window_joins, 1);
      } finally {
        db.close();
      }
    });
  }
});

test('report eligibility includes both endpoints and excludes adjacent decisions', (t) => {
  const w = world(t);
  const end = deployment + 30;
  w.append('before-start', deployment - 1);
  w.exit('before-start', deployment);
  w.append('at-start', deployment);
  w.exit('at-start', deployment + 1);
  w.append('at-end', end);
  w.exit('at-end', end);
  w.append('after-end', end + 1);
  w.exit('after-end', end + 1);

  const report = w.report(end);
  assert.equal(report.cumulative_accepted_bindings_present, 4);
  assert.equal(report.window_decisions, 2);
  assert.equal(report.accepted_window_joins, 2);
  assert.deepEqual(report.decisions.map((d) => [d.raw_decision_id, d.decision_ts]), [
    ['at-start', deployment], ['at-end', end],
  ]);
  const instant = w.report(end, end);
  assert.equal(instant.window_decisions, 1);
  assert.equal(instant.accepted_window_joins, 1);
  assert.equal(instant.decisions[0].raw_decision_id, 'at-end');
});

test('raw IDs are matched literally with case and punctuation preserved', (t) => {
  const w = world(t);
  const firstId = 'Decision.+:[1]';
  const secondId = 'decision.+:[1]';
  w.append(firstId, deployment + 10);
  w.append(secondId, deployment + 11);
  const second = w.exit(secondId, deployment + 20);
  const first = w.exit(firstId, deployment + 21);
  const report = w.report(deployment + 21);
  assert.equal(report.accepted_window_joins, 2);
  assert.deepEqual(report.decisions.map((d) => [
    d.raw_decision_id, d.source_run_id, d.completed_rounds[0].decision_id,
  ]), [
    [firstId, `relay-pacing:id:${firstId}`, first.decision_id],
    [secondId, `relay-pacing:id:${secondId}`, second.decision_id],
  ]);
  const db = new DatabaseSync(path.join(w.root, 'brain.db'));
  try {
    db.prepare('UPDATE routing_decisions_v2 SET causal_context_json=? WHERE decision_id=?')
      .run(JSON.stringify({ relay_decision_id: `${firstId}-suffix` }), first.decision_id);
    const mismatch = w.report(deployment + 21);
    assert.equal(mismatch.accepted_window_joins, 1);
    assert.equal(mismatch.decisions[0].status, 'no_completed_exit');
    assert.deepEqual(mismatch.decisions[0].accepted_edge_ids, []);
    assert.equal(mismatch.decisions[1].completed_rounds[0].decision_id, second.decision_id);
  } finally {
    db.close();
  }
});

test('report reads one snapshot across a concurrent WAL commit and preserves stored bytes', (t) => {
  const w = world(t);
  w.append('snapshot-decision', deployment + 10);
  const completed = w.exit('snapshot-decision', deployment + 20);
  const exits = path.join(w.root, 'relay-exits.log');
  fs.writeFileSync(exits, exitLine('snapshot-decision', deployment + 20));
  const writer = new DatabaseSync(path.join(w.root, 'brain.db'));
  try {
    assert.equal(writer.prepare('PRAGMA journal_mode=WAL').get().journal_mode, 'wal');
    const originalPrepare = DatabaseSync.prototype.prepare;
    let committed = false;
    // Commit on a separate connection after the first count read, before
    // reading decisions. No report implementation or data read is stubbed.
    const prepare = t.mock.method(DatabaseSync.prototype, 'prepare', function (sql) {
      if (!committed && this !== writer && sql.includes('SELECT event_id,run_id,created_ts')) {
        writer.prepare("UPDATE completion_events SET producer='foreign' WHERE run_id=?")
          .run(completed.decision_id);
        committed = true;
      }
      return originalPrepare.call(this, sql);
    });
    const snapshot = w.report(deployment + 20, deployment, w.log, exits);
    prepare.mock.restore();
    assert.equal(committed, true);
    assert.equal(snapshot.status, 'available');
    assert.equal(snapshot.cumulative_accepted_bindings_present, 1);
    assert.equal(snapshot.accepted_window_joins, 1);
    assert.equal(snapshot.raw_reconciliation.accepted_window_joins, 1);
    assert.equal(snapshot.decisions[0].completed_rounds[0].decision_id, completed.decision_id);

    // A fresh read sees the committed corruption. Reporting neither repairs it
    // nor writes the main database, WAL contents, or raw pacing log.
    const bytes = () => ['brain.db', 'brain.db-wal', 'relay-pacing.log', 'relay-exits.log'].map((file) =>
      fs.readFileSync(path.join(w.root, file)));
    const before = bytes();
    const fresh = w.report(deployment + 20, deployment, w.log, exits);
    assert.equal(fresh.status, 'available');
    assert.equal(fresh.window_decisions, 1);
    assert.equal(fresh.matching_completed_exit_decisions, 1);
    assert.equal(fresh.accepted_window_joins, 0);
    assert.equal(fresh.missing_join_decisions, 1);
    assert.equal(fresh.raw_reconciliation.unaccounted_completed_exit_decisions, 1);
    assert.deepEqual(w.report(deployment + 20, deployment, w.log, exits), fresh);
    assert.deepEqual(bytes(), before);
  } finally {
    writer.close();
  }
});
