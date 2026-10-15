'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;
const python = process.env.PYTHON || 'python3';
const observers = [
  'capability-activation-audit', 'capability-firing-monitor', 'capability-propensity',
];

// Exercise the commands the weekly review runs in fresh processes, using synthetic
// history in a private ledger. This is not a live mirror-sync acceptance witness.
function world(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'observer-verdict-cli-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const ledger = path.join(root, 'capabilities.json');
  const brain = path.join(root, 'unavailable.db');
  // A directory cannot be opened as SQLite: the Brain is explicitly unreadable,
  // rather than a missing store that the existing CLI initializes on demand.
  fs.mkdirSync(brain);
  const env = {
    ...process.env,
    PYTHONPATH: modules,
    ORCH_LOCAL_RUNTIME: root,
    ORCH_STATE_DIR: path.join(root, 'state'),
    ORCH_CAPABILITIES_PATH: ledger,
    ORCH_FEEDBACK_DB: brain,
    ORCH_CAPABILITY_HEARTBEATS: '0',
  };
  function run(args, expectedStatus = 0) {
    const output = path.join(root, 'stdout');
    const errors = path.join(root, 'stderr');
    const stdout = fs.openSync(output, 'w');
    const stderr = fs.openSync(errors, 'w');
    try {
      const result = spawnSync(python, args, {
        cwd: root, env, stdio: ['ignore', stdout, stderr], timeout: 30000,
      });
      assert.ifError(result.error);
      const err = fs.readFileSync(errors, 'utf8');
      assert.equal(result.status, expectedStatus, err);
      return { stdout: fs.readFileSync(output, 'utf8'), stderr: err };
    } finally {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
      fs.rmSync(output);
      fs.rmSync(errors);
    }
  }
  run(['-c', `
import time
import capabilities as caps
rows = {}
for cid, kind, cadence in (
    ('capability-activation-audit', 'tick_phase', 'daily'),
    ('capability-firing-monitor', 'tick_phase', 'weekly capability-firing-monitor'),
    ('capability-propensity', 'cli_subcommand', 'per advisor call'),
    ('issue-readiness', 'cli_subcommand', 'weekly issue-readiness'),
    ('offload', 'task_type', 'per task'),
):
    row = caps._blank_capability(cid)
    row.update(status='wired', matcher={'kind': kind}, trigger_cadence=cadence)
    rows[cid] = row
caps.save(rows)
for cid in ('capability-activation-audit', 'capability-firing-monitor', 'capability-propensity'):
    for index, useful in enumerate((True, False)):
        caps.heartbeat(cid, 'outcome', ref=f'advice:historical{index}',
            timestamp=int(time.time()) - 86400,
            metadata={'useful': useful, 'source': 'capability_propensity',
                      'verdict_kind': 'observer_output_change'})
`]);
  return {
    run, ledger, brain,
    usage: (...args) => run([path.join(modules, 'capabilities.py'), ...args, 'usage']).stdout,
    verdict: (cid, status) => run([
      path.join(modules, 'capability_propensity.py'), 'useful',
      '--capability', cid, '--experiment', 'advice:cli5840001',
      '--evidence', 'review isolated the demonstrated defect',
      '--provenance', 'machine_observed',
    ], status),
  };
}

test('usage CLI reclassifies observer history without rewriting outcome events', (t) => {
  const w = world(t);
  const before = JSON.parse(fs.readFileSync(w.ledger, 'utf8')).capabilities;
  const text = w.usage();
  for (const cid of [...observers, 'issue-readiness']) {
    const row = text.split('\n').find((line) => line.startsWith(`| ${cid} |`));
    assert.ok(row, `missing usage row: ${cid}`);
    const reason = cid === 'issue-readiness' ? 'tick_cadence' : 'observer';
    assert.ok(row.includes(`verdict n/a (${reason}); consumer actions unmeasured`), row);
    assert.ok(!row.includes('production useful') && !row.includes('promote it'), row);
  }
  assert.match(text, /\| offload \|.*production useful/);
  const usage = JSON.parse(w.usage('--json'));
  for (const cid of observers) {
    const row = usage.rows.find((r) => r.capability_id === cid);
    assert.equal(row.verdict_exempt_reason, 'observer');
    assert.equal(row.consumer_actions, null);
    assert.equal(row.observer_noise.useful, 1);
    assert.equal(row.observer_noise.not_useful, 1);
  }
  const after = JSON.parse(fs.readFileSync(w.ledger, 'utf8')).capabilities;
  for (const cid of Object.keys(before)) {
    assert.deepEqual(
      after[cid].event_history.filter((e) => e.type === 'outcome'),
      before[cid].event_history.filter((e) => e.type === 'outcome'),
    );
  }
  assert.deepEqual(fs.readdirSync(w.brain), []);
});

test('useful CLI refuses observer and cadence verdicts while recording delivery evidence', (t) => {
  const w = world(t);
  const before = fs.readFileSync(w.ledger);
  for (const cid of [...observers, 'issue-readiness']) {
    const { stderr } = w.verdict(cid, 2);
    const reason = cid === 'issue-readiness' ? 'tick_cadence' : 'observer';
    assert.ok(stderr.includes(`${cid}: usefulness verdict exempt (${reason})`), stderr);
    assert.ok(!stderr.includes('Traceback'), stderr);
    assert.deepEqual(fs.readFileSync(w.ledger), before);
  }
  w.verdict('offload', 0);
  const rows = JSON.parse(fs.readFileSync(w.ledger, 'utf8')).capabilities;
  assert.equal(rows.offload.event_history.filter((e) => e.type === 'outcome').length, 1);
  for (const cid of observers) {
    assert.equal(rows[cid].event_history.filter((e) => e.type === 'outcome').length, 2);
  }
  assert.deepEqual(fs.readdirSync(w.brain), []);
});
