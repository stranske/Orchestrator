'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;
const script = path.join(modules, 'adjudicator_retro.py');
const python = process.env.PYTHON || 'python3';
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

function world(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'native-inventory-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  // URI metacharacters must remain part of the selected journal's filename.
  const journal = path.join(root, 'native #1?.sqlite');
  const original = path.join(root, 'original.json');
  const canonical = path.join(root, 'canonical.json');
  const raw = 'private provider response';
  fs.writeFileSync(original, JSON.stringify({
    population: 299,
    rows: [{ case_id: 'original-a', decision: 'needs_more_evidence',
      backend_run_id: 'original-paid',
      retry_attempts: [{ retry_attempts: [{ backend_run_id: 'earlier-paid' }] }] }],
  }));
  const sourceHash = sha256(fs.readFileSync(original));
  const publication = JSON.parse(fs.readFileSync(original, 'utf8'));
  publication.native_publication = { source_report_sha256: sourceHash };
  publication.summary = { graded: 29 };
  publication.rows[0].native_projection = { trusted: true, graded: 1 };
  fs.writeFileSync(canonical, JSON.stringify(publication));

  function execute(args) {
    const out = path.join(root, 'stdout');
    const err = path.join(root, 'stderr');
    const stdout = fs.openSync(out, 'w');
    const stderr = fs.openSync(err, 'w');
    let result;
    try {
      result = spawnSync(python, args, {
        cwd: root, timeout: 15000,
        env: { ...process.env, PYTHONPATH: modules, PYTHONDONTWRITEBYTECODE: '1',
          ORCH_STATE_DIR: path.join(root, 'absent-state'),
          ORCH_LOCAL_RUNTIME: path.join(root, 'absent-runtime'),
          ORCH_FEEDBACK_DB: path.join(root, 'absent-brain.sqlite') },
        stdio: ['ignore', stdout, stderr],
      });
    } finally {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
    }
    assert.ifError(result.error);
    return { status: result.status, stdout: fs.readFileSync(out, 'utf8'),
      stderr: fs.readFileSync(err, 'utf8') };
  }
  function run(args) {
    const result = execute(args);
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
  }

  run(['-c', `
import json, sys
from pathlib import Path
from adjudicator_retro import NativeAttemptJournal
NativeAttemptJournal(Path(sys.argv[1]))
print(json.dumps(True))
`, journal]);
  const connection = new DatabaseSync(journal);
  t.after(() => connection.close());
  connection.exec('PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;');
  // Keep a reader open while the native producer commits, so its exit cannot
  // checkpoint and remove the WAL before the inventory process observes it.
  connection.exec('BEGIN');
  connection.prepare('SELECT COUNT(*) FROM native_attempts').get();
  const ids = run(['-c', `
import json, sys
from pathlib import Path
from adjudicator_retro import NativeAttemptJournal
journal = NativeAttemptJournal(Path(sys.argv[1]))
source = sys.argv[2]
reserved = journal.reserve('original-a', 'unstarted', 'digest-1', source, await_start=True)
started = journal.reserve('other-original', 'started', 'digest-2', source, await_start=True)
journal.begin_invocation(started['attempt_id'], {'run_id': 'unknown-paid'})
raw = journal.reserve('foreign', 'raw', 'digest-3', 'foreign-source')
journal.persist_raw(raw['attempt_id'], {'run_id': 'native-paid', 'output': sys.argv[3]})
print(json.dumps([item['attempt_id'] for item in (reserved, started, raw)]))
`, journal, sourceHash, raw]);
  connection.exec('COMMIT');
  const inventoryArgs = [script, '--inventory', '--report', original,
    '--canonical-report', canonical, '--native-journal', journal];
  const snapshot = () => [original, canonical, journal, `${journal}-wal`]
    .map((file) => [file, sha256(fs.readFileSync(file))]);
  return { root, journal, original, canonical, raw, ids, connection, sourceHash,
    execute, run, inventoryArgs, snapshot };
}

test('native inventory reads committed WAL receipts without checkpointing or granting authority', (t) => {
  const w = world(t);
  // Copying just the database would lose every receipt in this observation.
  const stale = path.join(w.root, 'main-file-only.sqlite');
  fs.copyFileSync(w.journal, stale);
  const mainOnly = new DatabaseSync(stale, { readOnly: true });
  assert.equal(mainOnly.prepare('SELECT COUNT(*) AS n FROM native_attempts').get().n, 0);
  mainOnly.close();
  assert.equal(w.connection.prepare('SELECT COUNT(*) AS n FROM native_attempts').get().n, 3);
  const before = w.snapshot();
  const report = w.run(w.inventoryArgs);
  assert.deepEqual(w.snapshot(), before, 'inventory must not checkpoint or rewrite evidence');
  assert.equal(report.original_report.sha256, w.sourceHash);
  assert.equal(report.native_journal.path, w.journal);
  assert.deepEqual(report.original_paid_attempts.map((item) => item.backend_run_id),
    ['original-paid', 'earlier-paid']);
  const attempts = new Map(report.native_journal.attempts.map((item) => [item.attempt_id, item]));
  assert.deepEqual(w.ids.map((id) => attempts.get(id).state),
    ['reserved', 'in_flight', 'raw_persisted']);
  assert.equal(attempts.get(w.ids[1]).backend_run_id, 'unknown-paid');
  assert.equal(attempts.get(w.ids[2]).raw_response_sha256, sha256(w.raw));
  assert.equal(attempts.get(w.ids[2]).matches_original_source, false);
  assert.equal(attempts.get(w.ids[2]).in_original_cohort, false);
  assert.equal(report.paid_identity_summary.union_unique, 4);
  assert.equal(report.row_diagnostics[0].unresolved_native_attempts[0].outcome, 'UNKNOWN');
  assert.equal(report.canonical_report.retained_summary.graded, 29);
  assert.equal(report.canonical_report.summary_authority, 'not_authenticated_by_inventory');
  assert.equal(report.dispatch_authorized, false);
  assert.equal(report.historical_observations.startsWith('unavailable'), true);
  for (const item of attempts.values()) {
    assert.equal(item.receipt_authority, 'not_authenticated_by_inventory');
  }
  assert.equal(JSON.stringify(report).includes(w.raw), false);
  for (const name of ['absent-state', 'absent-runtime', 'absent-brain.sqlite']) {
    assert.equal(fs.existsSync(path.join(w.root, name)), false);
  }
});

test('native inventory separates uncommitted provider changes from a later committed observation', (t) => {
  const w = world(t);
  const before = w.snapshot();
  const initial = w.run(w.inventoryArgs);
  w.connection.exec('BEGIN IMMEDIATE');
  try {
    w.connection.prepare('UPDATE native_attempts SET raw_response=? WHERE attempt_id=?')
      .run('later private provider response', w.ids[2]);
    const concurrent = w.run(w.inventoryArgs);
    assert.deepEqual(concurrent.native_journal, initial.native_journal);
    assert.deepEqual(w.snapshot(), before);
    w.connection.exec('COMMIT');
  } finally {
    if (w.connection.isTransaction) w.connection.exec('ROLLBACK');
  }
  const committed = w.snapshot();
  const later = w.run(w.inventoryArgs);
  const receipt = later.native_journal.attempts.find((item) => item.attempt_id === w.ids[2]);
  assert.equal(receipt.raw_response_sha256, sha256('later private provider response'));
  assert.notDeepEqual(later.native_journal, initial.native_journal);
  assert.deepEqual(w.snapshot(), committed);
  assert.equal(later.dispatch_authorized, false);
});

test('native inventory rejects dispatch and retry before touching a live WAL journal', (t) => {
  const w = world(t);
  const before = w.snapshot();
  for (const mode of ['--dispatch', '--retry', '--native-cohort', '--selftest']) {
    const result = w.execute([...w.inventoryArgs, mode]);
    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, /--inventory is read-only/);
    assert.equal(result.stdout, '');
    assert.deepEqual(w.snapshot(), before);
  }
});
