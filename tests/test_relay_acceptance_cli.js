'use strict';

// Synthetic CLI witnesses only; these are not production acceptance receipts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const moduleDir = fs.existsSync(path.join(repo, 'src', 'rate_incidents.py'))
  ? path.join(repo, 'src') : repo;
const python = process.env.PYTHON || 'python3';
const deployment = 1793693792; // 2026-11-03T08:16:32Z, the recorded deployment boundary.

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
  function run(args) {
    // File capture works on hosts that disallow child-process pipes.
    const output = path.join(root, 'stdout');
    const errors = path.join(root, 'stderr');
    const stdout = fs.openSync(output, 'w');
    const stderr = fs.openSync(errors, 'w');
    try {
      const result = spawnSync(python, args, {
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
