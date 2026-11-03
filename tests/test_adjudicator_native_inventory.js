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

  function execute(args, environment = {}) {
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
          ORCH_FEEDBACK_DB: path.join(root, 'absent-brain.sqlite'), ...environment },
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
  function run(args, environment) {
    const result = execute(args, environment);
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

function git(root, ...args) {
  // Fixture setup must use its own repositories even inside a CI Git hook.
  const environment = Object.fromEntries(Object.entries(process.env)
    .filter(([name]) => !name.startsWith('GIT_')));
  const out = path.join(root, 'git-stdout');
  const err = path.join(root, 'git-stderr');
  const stdout = fs.openSync(out, 'w');
  const stderr = fs.openSync(err, 'w');
  let result;
  try {
    result = spawnSync('git', ['-C', root,
      '-c', 'user.name=Inventory Test', '-c', 'user.email=inventory@example.invalid', ...args], {
      timeout: 15000, env: environment, stdio: ['ignore', stdout, stderr],
    });
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
  }
  assert.ifError(result.error);
  assert.equal(result.status, 0, fs.readFileSync(err, 'utf8'));
  return fs.readFileSync(out, 'utf8').trim();
}

function deployment(w) {
  const root = path.join(w.root, 'deployment');
  const modules = path.join(root, 'src');
  fs.mkdirSync(modules, { recursive: true });
  fs.writeFileSync(path.join(modules, 'adjudicator_retro.py'), '# deployed retrospective\n');
  fs.writeFileSync(path.join(modules, 'roles.py'), '# deployed validator\n');
  git(root, 'init', '-q');
  git(root, 'add', 'src');
  git(root, 'commit', '-qm', 'Deployment fixture');
  return { root, modules, revision: git(root, 'rev-parse', 'HEAD') };
}

function moduleHashes(root) {
  return Object.fromEntries(fs.readdirSync(root).filter((name) => name.endsWith('.py'))
    .map((name) => [name, sha256(fs.readFileSync(path.join(root, name)))]));
}

test('inventory CLI keeps deployment and a flat mirror generation separately attributable', (t) => {
  const w = world(t);
  const deployed = deployment(w);
  // The flat generation is nested inside a checkout but owns no Git metadata.
  const generation = path.join(deployed.root, 'generation-a');
  fs.mkdirSync(generation);
  fs.writeFileSync(path.join(generation, 'adjudicator_retro.py'), '# mirrored retrospective\n');
  fs.writeFileSync(path.join(generation, 'roles.py'), '# mirrored validator\n');
  const mirror = path.join(w.root, 'published-mirror');
  fs.symlinkSync(generation, mirror, 'dir');
  const before = w.snapshot();
  const args = [...w.inventoryArgs, '--deployed-root', deployed.root, '--mirror-root', mirror];
  const report = w.run(args);
  assert.equal(report.deployed_code.git_revision, deployed.revision);
  assert.equal(report.deployed_code.tracked_files_clean, true);
  assert.equal(report.deployed_code.resolved_path, fs.realpathSync(deployed.root));
  assert.deepEqual(report.deployed_code.runtime_module_sha256, moduleHashes(deployed.modules));
  assert.equal(report.mirror_code.path, mirror);
  assert.equal(report.mirror_code.resolved_path, fs.realpathSync(generation));
  assert.equal(report.mirror_code.git_revision, null, 'a flat mirror cannot inherit its parent HEAD');
  assert.equal(report.mirror_code.tracked_files_clean, null);
  assert.deepEqual(report.mirror_code.runtime_module_sha256, moduleHashes(generation));
  assert.notEqual(report.deployed_code.adjudicator_sha256, report.mirror_code.adjudicator_sha256);
  for (const observed of [report.deployed_code, report.mirror_code]) {
    assert.equal(observed.adjudicator_sha256, observed.runtime_module_sha256['adjudicator_retro.py']);
    assert.equal(observed.live_reader_binding, 'unverified');
  }
  assert.equal(report.evidence_class, 'current_local_inventory_only');
  assert.equal(report.historical_observations.startsWith('unavailable'), true);
  assert.equal(report.dispatch_authorized, false);
  assert.deepEqual(w.snapshot(), before);

  // HEAD alone cannot identify a later local validator edit.
  fs.writeFileSync(path.join(deployed.modules, 'roles.py'), '# locally edited validator\n');
  const later = w.run(args);
  assert.equal(later.deployed_code.git_revision, deployed.revision);
  assert.equal(later.deployed_code.tracked_files_clean, false);
  assert.notDeepEqual(later.deployed_code.runtime_module_sha256,
    report.deployed_code.runtime_module_sha256);
  assert.deepEqual(later.mirror_code, report.mirror_code);
  assert.deepEqual(later.native_journal, report.native_journal);
  assert.deepEqual(w.snapshot(), before);
});

test('inventory CLI pins a dirty mirror worktree despite inherited deployment Git settings', (t) => {
  const w = world(t);
  const deployed = deployment(w);
  const mirror = path.join(w.root, 'mirror-worktree');
  git(deployed.root, 'worktree', 'add', '-q', '-b', 'mirror-fixture', mirror);
  const mirrorRevision = git(mirror, 'rev-parse', 'HEAD');
  git(deployed.root, 'commit', '--allow-empty', '-qm', 'Later deployment');
  const deployedRevision = git(deployed.root, 'rev-parse', 'HEAD');
  assert.notEqual(deployedRevision, mirrorRevision);
  assert.equal(fs.statSync(path.join(mirror, '.git')).isFile(), true);
  fs.writeFileSync(path.join(mirror, 'src', 'roles.py'), '# mirror-local validator\n');
  const before = w.snapshot();
  const environment = {
    GIT_DIR: path.join(deployed.root, '.git'), GIT_WORK_TREE: deployed.root,
    GIT_COMMON_DIR: path.join(deployed.root, '.git'),
    GIT_INDEX_FILE: path.join(deployed.root, '.git', 'index'),
  };
  const args = [...w.inventoryArgs, '--deployed-root', deployed.root, '--mirror-root', mirror];
  const report = w.run(args, environment);
  assert.equal(report.deployed_code.git_revision, deployedRevision);
  assert.equal(report.deployed_code.tracked_files_clean, true);
  assert.equal(report.mirror_code.git_revision, mirrorRevision);
  assert.equal(report.mirror_code.tracked_files_clean, false);
  assert.deepEqual(report.mirror_code.runtime_module_sha256, moduleHashes(path.join(mirror, 'src')));
  assert.equal(report.mirror_code.live_reader_binding, 'unverified');
  assert.deepEqual(report.original_report.case_ids, ['original-a']);
  assert.deepEqual(report.original_paid_attempts.map((item) => item.backend_run_id),
    ['original-paid', 'earlier-paid']);
  assert.equal(report.row_diagnostics[0].unresolved_native_attempts[0].outcome, 'UNKNOWN');
  assert.equal(report.canonical_report.summary_authority, 'not_authenticated_by_inventory');
  assert.equal(report.dispatch_authorized, false);
  assert.deepEqual(w.snapshot(), before);
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

for (const transition of ['begin_invocation', 'persist_raw']) {
  test(`native inventory rejects ${transition} committed to WAL during observation`, (t) => {
    const w = world(t);
    const before = w.snapshot();
    const attemptId = w.ids[transition === 'begin_invocation' ? 0 : 1];
    const commitSnapshot = path.join(w.root, 'commit-snapshot.json');
    // The first code observation is after the initial journal read. Commit
    // through the native producer at that boundary, without a provider call.
    const result = w.execute(['-c', `
import hashlib, json, sys
from pathlib import Path
import adjudicator_retro as retro

writer = retro.NativeAttemptJournal.__new__(retro.NativeAttemptJournal)
writer.path = Path(sys.argv[1])
attempt_id, transition = sys.argv[2:4]
snapshot_path = Path(sys.argv[4])
observe = retro._inventory_code
committed = False

def observe_after_commit(root):
    global committed
    result = observe(root)
    if not committed:
        committed = True
        if transition == 'begin_invocation':
            writer.begin_invocation(attempt_id, {'run_id': 'concurrent-paid'})
        else:
            writer.persist_raw(attempt_id, {
                'run_id': 'unknown-paid', 'output': 'concurrent private receipt',
            })
        snapshot_path.write_text(json.dumps([
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (writer.path, Path(str(writer.path) + '-wal'))
        ]))
    return result

retro._inventory_code = observe_after_commit
sys.argv = ['adjudicator_retro.py', *sys.argv[5:]]
raise SystemExit(retro.main())
`, w.journal, attemptId, transition, commitSnapshot, ...w.inventoryArgs.slice(1)]);
    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, /native inventory inputs changed during observation/);
    assert.equal(result.stdout, '', 'a mixed observation must not emit an inventory');
    const [mainHash, walHash] = JSON.parse(fs.readFileSync(commitSnapshot, 'utf8'));
    assert.equal(mainHash, before[2][1], 'the producer must not checkpoint before the recheck');
    assert.notEqual(walHash, before[3][1], 'the producer commit must reside in WAL');
    // Producer process cleanup may checkpoint afterward. Compare the resulting
    // durable files; a subsequent SQLite read may create an empty WAL sidecar.
    const snapshot = () => before.slice(0, 3).map(([file]) =>
      [file, sha256(fs.readFileSync(file))]);
    const committed = snapshot();
    assert.deepEqual(committed.slice(0, 2), before.slice(0, 2));

    // Rejection must preserve the paid boundary/receipt for the next inventory.
    const later = w.run(w.inventoryArgs);
    const attempt = later.native_journal.attempts.find((item) => item.attempt_id === attemptId);
    assert.equal(later.native_journal.attempts.length, 3);
    assert.equal(attempt.matches_original_source, true);
    if (transition === 'begin_invocation') {
      assert.equal(attempt.state, 'in_flight');
      assert.equal(attempt.backend_run_id, 'concurrent-paid');
      assert.equal(later.paid_identity_summary.union_unique, 5);
      assert.equal(later.row_diagnostics[0].unresolved_native_attempts[0].outcome, 'UNKNOWN');
    } else {
      assert.equal(attempt.state, 'raw_persisted');
      assert.equal(attempt.backend_run_id, 'unknown-paid');
      assert.equal(attempt.raw_response_sha256, sha256('concurrent private receipt'));
      assert.equal(later.paid_identity_summary.union_unique, 4);
      assert.equal(JSON.stringify(later).includes('concurrent private receipt'), false);
    }
    assert.deepEqual(later.original_paid_attempts.map((item) => item.backend_run_id),
      ['original-paid', 'earlier-paid']);
    assert.equal(later.dispatch_authorized, false);
    assert.equal(attempt.receipt_authority, 'not_authenticated_by_inventory');
    assert.deepEqual(snapshot(), committed);
    for (const name of ['absent-state', 'absent-runtime', 'absent-brain.sqlite']) {
      assert.equal(fs.existsSync(path.join(w.root, name)), false);
    }
  });
}

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
