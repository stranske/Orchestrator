'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { test } = require('node:test');
const { reportInventory } = require('../scripts/export_adjudicator_inventory');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;
const script = path.join(modules, 'adjudicator_retro.py');
const python = process.env.PYTHON || 'python3';
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

function world(t, rows = [{ case_id: 'original-a', decision: 'needs_more_evidence',
  backend_run_id: 'original-paid',
  retry_attempts: [{ retry_attempts: [{ backend_run_id: 'earlier-paid' }] }] }],
  journalName = 'native #1?.sqlite') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'native-inventory-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  // URI metacharacters must remain part of the selected journal's filename.
  const journal = path.join(root, journalName);
  const original = path.join(root, 'original.json');
  const canonical = path.join(root, 'canonical.json');
  const raw = 'private provider response';
  fs.writeFileSync(original, JSON.stringify({
    population: 299,
    rows,
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

test('retained inventory raw hashes match native producer receipts and the Python inventory', (t) => {
  const w = world(t, [{ case_id: 'original-a', target: 'owner/repo#1',
    decision: 'needs_more_evidence', backend_run_id: 'original-paid',
    retry_attempts: [{ retry_attempts: [{ backend_run_id: 'earlier-paid' }] }] }]);
  const texts = ['', 'private café\r\n', 'private cafe\u0301\n'];
  const ids = w.run(['-c', `
import json, sys
from pathlib import Path
from adjudicator_retro import NativeAttemptJournal
journal = NativeAttemptJournal(Path(sys.argv[1]))
ids = []
for index, text in enumerate(json.loads(sys.argv[3])):
    record = journal.reserve('receipt-case-' + str(index), 'receipt-' + str(index), 'digest', sys.argv[2])
    assert record['reserved']
    journal.persist_raw(record['attempt_id'], {'run_id': 'receipt-paid-' + str(index), 'output': text})
    ids.append(record['attempt_id'])
print(json.dumps(ids))
`, w.journal, w.sourceHash, JSON.stringify(texts)]);
  // A well-formed foreign source hash remains attributable independently of
  // the native producer's current-source receipts. Malformed hashes are
  // covered separately by the retained-report API/CLI regressions.
  w.connection.prepare('UPDATE native_attempts SET report_sha256=? WHERE attempt_id=?')
    .run('b'.repeat(64), w.ids[2]);
  const before = w.snapshot();
  const native = w.run(w.inventoryArgs);
  const retained = reportInventory({ report: w.original, journal: w.journal,
    observedAt: 2000000000, evidenceClass: 'current_snapshot',
    evidenceReference: 'isolated-native-producer-fixture' });
  const attempts = retained.pre_dispatch.native_journal.attempts;
  assert.deepEqual(retained.pre_dispatch.source_lineage, {
    reference_kind: 'observed_report_bytes', reference_sha256: w.sourceHash,
    original_source_authenticated: false,
  });
  for (const record of native.native_journal.attempts) {
    const observed = attempts.find((row) => row.attempt_id === record.attempt_id);
    assert.equal(observed.raw_response_sha256, record.raw_response_sha256,
      'both inventory paths must identify the same retained bytes');
    assert.deepEqual(observed.source_binding, {
      status: record.matches_original_source ? 'matches_reference' : 'differs_from_reference',
      authenticated: false,
    });
  }
  const canonical = reportInventory({ report: w.canonical, journal: w.journal,
    observedAt: 2000000000, evidenceClass: 'current_snapshot',
    evidenceReference: 'isolated-native-publication-fixture' });
  assert.deepEqual(canonical.pre_dispatch.source_lineage, {
    ...retained.pre_dispatch.source_lineage, reference_kind: 'retained_native_publication_marker',
  });
  assert.deepEqual(canonical.pre_dispatch.native_journal, retained.pre_dispatch.native_journal);
  assert.equal(canonical.receipt_authority, 'not_authenticated_by_inventory');
  assert.equal(canonical.dispatch_authorized, false);
  ids.forEach((id, index) => {
    assert.equal(attempts.find((row) => row.attempt_id === id).raw_response_sha256, sha256(texts[index]));
  });
  assert.deepEqual(retained.paid_attempts.map((row) => row.backend_run_id),
    ['original-paid', 'earlier-paid']);
  assert.equal(retained.pre_dispatch.paid_identity_reconciliation.union_unique, 7);
  assert.equal(native.row_diagnostics[0].unresolved_native_attempts[0].outcome, 'UNKNOWN');
  assert.equal(retained.receipt_authority, 'not_authenticated_by_inventory');
  assert.equal(retained.dispatch_authorized, false);
  assert.ok(!JSON.stringify(retained).includes('private'));
  assert.deepEqual(w.snapshot(), before);
});

for (const field of ['backend_usage', 'backend_error']) {
  test(`retained inventory detects committed ${field} changes without publishing provider metadata`, (t) => {
    const w = world(t, [{ case_id: 'original-a', target: 'owner/repo#1',
      decision: 'needs_more_evidence', backend_run_id: 'original-paid',
      retry_attempts: [{ retry_attempts: [{ backend_run_id: 'earlier-paid' }] }] }]);
    const options = { report: w.original, journal: w.journal, observedAt: 2000000000,
      evidenceClass: 'current_snapshot', evidenceReference: 'isolated-native-metadata-fixture' };
    const initial = reportInventory(options);
    const before = w.snapshot();
    const mainInode = fs.statSync(w.journal, { bigint: true }).ino;
    const value = field === 'backend_usage'
      ? JSON.stringify({ cost_usd: 0.75, detail: 'private provider usage' })
      : 'private provider error';
    const update = w.connection.prepare(`UPDATE native_attempts SET ${field}=? WHERE attempt_id=?`);
    const stored = () => w.connection.prepare(
      `SELECT ${field} AS value FROM native_attempts WHERE attempt_id=?`).get(w.ids[2]).value;
    const original = stored();
    for (const committed of [false, true]) {
      const read = fs.readFileSync;
      let reportReads = 0;
      let committedSnapshot;
      fs.readFileSync = function observe(file, ...args) {
        const bytes = read(file, ...args);
        if (file === w.original && ++reportReads === 2) {
          w.connection.exec('BEGIN IMMEDIATE');
          update.run(value, w.ids[2]);
          if (committed) {
            w.connection.exec('COMMIT');
            committedSnapshot = before.map(([file]) => [file, sha256(read(file))]);
          }
        }
        return bytes;
      };
      try {
        if (committed) {
          assert.throws(() => reportInventory(options), /native journal receipts changed during inventory/);
        } else {
          assert.deepEqual(reportInventory(options), initial, 'uncommitted metadata is invisible');
        }
      } finally {
        fs.readFileSync = read;
        if (w.connection.isTransaction) w.connection.exec('ROLLBACK');
      }
      assert.equal(reportReads, 2, 'metadata changes after the initial receipt read');
      assert.equal(fs.statSync(w.journal, { bigint: true }).ino, mainInode);
      if (committed) {
        assert.equal(stored(), value);
        assert.deepEqual(committedSnapshot.slice(0, 3), before.slice(0, 3), 'commit stays in WAL');
        assert.notEqual(committedSnapshot[3][1], before[3][1]);
        // The new snapshot digest attributes private metadata changes while
        // published receipt locators and paid accounting remain unchanged.
        const later = reportInventory(options);
        const hash = later.pre_dispatch.native_journal.snapshot_sha256;
        assert.notEqual(hash, initial.pre_dispatch.native_journal.snapshot_sha256);
        assert.equal(hash, sha256(JSON.stringify(w.connection.prepare(
          'SELECT * FROM native_attempts ORDER BY attempt_id').all())));
        const expected = structuredClone(initial);
        expected.pre_dispatch.native_journal.snapshot_sha256 = hash;
        assert.deepEqual(later, expected);
        assert.equal(later.dispatch_authorized, false);
        assert.equal(later.receipt_authority, 'not_authenticated_by_inventory');
        assert.ok(!JSON.stringify(later).includes('private'));
        assert.deepEqual(w.snapshot(), committedSnapshot);
      } else {
        assert.equal(stored(), original);
        assert.deepEqual(w.snapshot(), before);
      }
    }
  });
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

test('requested generic retry stops at the native canonical before any evidence or paid work', (t) => {
  const w = world(t);
  const state = path.join(w.root, 'selected-state');
  const selected = path.join(state, 'capability-program', 'adjudicator-retro.json');
  fs.mkdirSync(path.dirname(selected), { recursive: true });
  fs.symlinkSync(w.canonical, selected);
  const before = w.snapshot();
  const flags = ['--dispatch', '--retry', '--limit', '21'];
  const environment = { ORCH_STATE_DIR: state };
  // Exercise the actual entry point as well as a guarded run that proves the
  // refusal precedes independent truth reads, history repair and dispatch.
  const results = [w.execute([script, ...flags], environment), w.execute(['-c', `
import sys
import adjudicator_retro as retro

def forbidden(*args, **kwargs):
    raise AssertionError('retry crossed the canonical publication boundary')

for name in ('disputes', '_refresh_saved_verdicts', 'fetch_evidence',
             '_persist_report', 'native_later_truth', 'measured_cost'):
    setattr(retro, name, forbidden)
retro.roles.run_adjudicator_agent = forbidden
retro.roles.dispatcher.offload = forbidden
retro.feedback._conn = forbidden
retro.feedback.record_role_run = forbidden
retro.NativeAttemptJournal.__init__ = forbidden
sys.argv = ['adjudicator_retro.py', *sys.argv[1:]]
raise SystemExit(retro.main())
`, ...flags], environment)];
  for (const result of results) {
    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, /generic retrospective run refuses to overwrite native canonical publication/);
    assert.match(result.stderr, /--native-admit/);
    assert.match(result.stderr, /--native-publish/);
    assert.doesNotMatch(result.stderr, /crossed the canonical publication boundary/);
    assert.equal(result.stdout, '', 'a refused retry cannot claim a refreshed summary');
  }
  assert.equal(fs.realpathSync(selected), fs.realpathSync(w.canonical));
  assert.equal(fs.existsSync(path.join(state, '.orchestrator-mirror.publish.lock')), false);
  assert.deepEqual(w.snapshot(), before);
  const inventory = w.run(w.inventoryArgs);
  assert.deepEqual(inventory.original_paid_attempts.map((row) => row.backend_run_id),
    ['original-paid', 'earlier-paid']);
  assert.equal(inventory.row_diagnostics[0].unresolved_native_attempts[0].outcome, 'UNKNOWN');
  assert.equal(inventory.canonical_report.summary_authority, 'not_authenticated_by_inventory');
  assert.equal(inventory.dispatch_authorized, false);
  assert.deepEqual(w.snapshot(), before);
  for (const name of ['absent-runtime', 'absent-brain.sqlite']) {
    assert.equal(fs.existsSync(path.join(w.root, name)), false);
  }
});

for (const stale of ['source', 'canonical']) {
  test(`native publication CLI rejects a stale ${stale} pin before refresh or dispatch`, (t) => {
    const w = world(t, undefined, 'native.sqlite');
    // No enrolled cases are needed to test admission. Stale pins must fail
    // before projection can read independent outcomes or write a candidate.
    const manifest = path.join(w.root, 'cohort.json');
    fs.writeFileSync(manifest, '[]');
    const before = w.snapshot();
    const sourceHash = stale === 'source' ? '0'.repeat(64) : w.sourceHash;
    const canonicalHash = stale === 'canonical' ? '0'.repeat(64)
      : sha256(fs.readFileSync(w.canonical));
    const result = w.execute(['-c', `
import sys
import adjudicator_retro as retro

def forbidden(*args, **kwargs):
    raise AssertionError('stale pin crossed the native publication boundary')

retro.refresh_native_cohort = forbidden
retro.native_later_truth = forbidden
retro.roles.run_adjudicator_agent = forbidden
retro.roles.dispatcher.offload = forbidden
retro.feedback._conn = forbidden
retro.feedback.record_role_run = forbidden
retro.NativeAttemptJournal.__init__ = forbidden
retro.NativeAttemptJournal.reserve = forbidden
sys.argv = ['adjudicator_retro.py', *sys.argv[1:]]
raise SystemExit(retro.main())
`, '--native-publish', w.canonical, '--report', w.original,
    '--native-journal', w.journal, '--cohort-manifest', manifest,
    '--expected-source-hash', sourceHash, '--expected-canonical-hash', canonicalHash]);
    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, stale === 'source'
      ? /original report changed before native publication/
      : /canonical report changed before native publication/);
    assert.doesNotMatch(result.stderr, /crossed the native publication boundary/);
    assert.equal(result.stdout, '', 'a stale publication must not emit a success receipt');
    assert.deepEqual(w.snapshot(), before);
    assert.equal(fs.readFileSync(manifest, 'utf8'), '[]');
    assert.equal(fs.existsSync(path.join(w.root, '.orchestrator-mirror.publish.lock')), false);
    for (const name of ['absent-state', 'absent-runtime', 'absent-brain.sqlite']) {
      assert.equal(fs.existsSync(path.join(w.root, name)), false);
    }
  });
}

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

for (const change of ['deployment_revision', 'deployment_module', 'mirror_generation']) {
  test(`inventory CLI refuses ${change} changes during the deployment/mirror observation`, (t) => {
    const w = world(t);
    const deployed = deployment(w);
    const generation = path.join(w.root, 'generation-a');
    const replacement = path.join(w.root, 'generation-b');
    for (const directory of [generation, replacement]) {
      fs.mkdirSync(directory);
      fs.writeFileSync(path.join(directory, 'adjudicator_retro.py'), '# mirrored retrospective\n');
      fs.writeFileSync(path.join(directory, 'roles.py'), '# mirrored validator\n');
    }
    const mirror = path.join(w.root, 'published-mirror');
    fs.symlinkSync(generation, mirror, 'dir');
    const args = [...w.inventoryArgs, '--deployed-root', deployed.root, '--mirror-root', mirror];
    const initial = w.run(args);
    const before = w.snapshot();
    const result = w.execute(['-c', `
import os, subprocess, sys
from pathlib import Path
import adjudicator_retro as retro

deployed, mirror, replacement = map(Path, sys.argv[1:4])
change = sys.argv[4]
observe = retro._inventory_code
observations = 0

def observe_then_change(root):
    global observations
    result = observe(root)
    observations += 1
    # Both initial pins have been captured. Change one before the final
    # comparison, leaving reports and committed WAL receipts untouched.
    if observations == 2:
        if change == 'deployment_revision':
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith('GIT_')}
            subprocess.run(['git', '-C', str(deployed),
                            '-c', 'user.name=Inventory Test',
                            '-c', 'user.email=inventory@example.invalid',
                            'commit', '--allow-empty', '-qm', 'Deployment advanced'],
                           check=True, env=environment)
        elif change == 'deployment_module':
            (deployed / 'src/roles.py').write_text('# changed deployment validator\\n')
        else:
            temporary = mirror.with_name(mirror.name + '.next')
            temporary.symlink_to(replacement, target_is_directory=True)
            temporary.replace(mirror)
    return result

retro._inventory_code = observe_then_change
sys.argv = ['adjudicator_retro.py', *sys.argv[5:]]
raise SystemExit(retro.main())
`, deployed.root, mirror, replacement, change, ...args.slice(1)]);
    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, /runtime code location changed during inventory/);
    assert.equal(result.stdout, '', 'changing code pins must not emit a partial inventory');
    assert.deepEqual(w.snapshot(), before);

    // A subsequent stable observation identifies the change while retaining
    // the independent original, native and foreign paid-attempt evidence.
    const later = w.run(args);
    if (change === 'deployment_revision') {
      assert.notEqual(later.deployed_code.git_revision, initial.deployed_code.git_revision);
      assert.equal(later.deployed_code.tracked_files_clean, true);
      assert.deepEqual(later.deployed_code.runtime_module_sha256,
        initial.deployed_code.runtime_module_sha256);
    } else if (change === 'deployment_module') {
      assert.equal(later.deployed_code.git_revision, initial.deployed_code.git_revision);
      assert.equal(later.deployed_code.tracked_files_clean, false);
      assert.notEqual(later.deployed_code.runtime_module_sha256['roles.py'],
        initial.deployed_code.runtime_module_sha256['roles.py']);
    } else {
      assert.equal(later.mirror_code.resolved_path, replacement);
      assert.notEqual(later.mirror_code.resolved_path, initial.mirror_code.resolved_path);
      assert.deepEqual(later.mirror_code.runtime_module_sha256,
        initial.mirror_code.runtime_module_sha256, 'identical code does not identify a generation');
    }
    assert.deepEqual(later.original_report, initial.original_report);
    assert.deepEqual(later.canonical_report, initial.canonical_report);
    assert.deepEqual(later.original_paid_attempts, initial.original_paid_attempts);
    assert.deepEqual(later.native_journal, initial.native_journal);
    assert.deepEqual(later.paid_identity_summary, initial.paid_identity_summary);
    assert.equal(later.row_diagnostics[0].unresolved_native_attempts[0].outcome, 'UNKNOWN');
    assert.equal(later.deployed_code.live_reader_binding, 'unverified');
    assert.equal(later.mirror_code.live_reader_binding, 'unverified');
    assert.equal(later.canonical_report.summary_authority, 'not_authenticated_by_inventory');
    assert.match(later.historical_observations, /^unavailable/);
    assert.equal(later.dispatch_authorized, false);
    assert.ok(!JSON.stringify(later).includes(w.raw));
    assert.deepEqual(w.snapshot(), before);
  });
}

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

for (const transition of ['begin_invocation', 'persist_raw', 'validate']) {
  test(`native inventory rejects ${transition} committed to WAL during observation`, (t) => {
    const w = world(t);
    const proposal = {
      decision: 'reject_blocker', confidence: 'high',
      rationale: 'The supplied evidence contradicts the blocker.',
      ground_truth_refs: ['git-path:proof.txt'],
      recommended_next_step: 'Inspect the proof before acting.', evidence_gaps: [],
      evidence_assessment: [{ claim: 'proof missing', status: 'contradicted',
        evidence_ref: 'git-path:proof.txt', reason: 'Proof contents supplied.' }],
    };
    const validated = { backend_run_id: 'validated-paid', raw_output: JSON.stringify(proposal),
      proposal, errors: [] };
    const attemptId = w.ids[transition === 'persist_raw' ? 1 : 0];
    if (transition === 'validate') {
      // Keep this original-cohort attempt in raw_persisted until inventory has
      // read it. The producer owns every transition; no SQL state fabrication.
      w.run(['-c', `
import json, sys
from pathlib import Path
from adjudicator_retro import NativeAttemptJournal
journal = NativeAttemptJournal(Path(sys.argv[1]))
result = json.loads(sys.argv[3])
journal.begin_invocation(sys.argv[2], {'run_id': result['backend_run_id']})
journal.persist_raw(sys.argv[2], {
    'run_id': result['backend_run_id'], 'output': result['raw_output'],
})
print(json.dumps(True))
`, w.journal, attemptId, JSON.stringify(validated)]);
    }
    const before = w.snapshot();
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
validated_result = json.loads(sys.argv[5])
observe = retro._inventory_code
committed = False

def observe_after_commit(root):
    global committed
    result = observe(root)
    if not committed:
        committed = True
        if transition == 'begin_invocation':
            writer.begin_invocation(attempt_id, {'run_id': 'concurrent-paid'})
        elif transition == 'persist_raw':
            writer.persist_raw(attempt_id, {
                'run_id': 'unknown-paid', 'output': 'concurrent private receipt',
            })
        else:
            assert not retro.roles.ROLE_REGISTRY['adjudicator'].validate(validated_result['proposal'])
            writer.validate(attempt_id, validated_result)
        snapshot_path.write_text(json.dumps([
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (writer.path, Path(str(writer.path) + '-wal'))
        ]))
    return result

retro._inventory_code = observe_after_commit
sys.argv = ['adjudicator_retro.py', *sys.argv[6:]]
raise SystemExit(retro.main())
`, w.journal, attemptId, transition, commitSnapshot, JSON.stringify(validated),
    ...w.inventoryArgs.slice(1)]);
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
    } else if (transition === 'persist_raw') {
      assert.equal(attempt.state, 'raw_persisted');
      assert.equal(attempt.backend_run_id, 'unknown-paid');
      assert.equal(attempt.raw_response_sha256, sha256('concurrent private receipt'));
      assert.equal(later.paid_identity_summary.union_unique, 4);
      assert.equal(JSON.stringify(later).includes('concurrent private receipt'), false);
    } else {
      assert.equal(attempt.state, 'validated');
      assert.equal(attempt.backend_run_id, validated.backend_run_id);
      assert.equal(attempt.raw_response_sha256, sha256(validated.raw_output));
      const retained = w.connection.prepare(
        'SELECT validated_json FROM native_attempts WHERE attempt_id=?').get(attemptId);
      assert.deepEqual(JSON.parse(retained.validated_json), validated);
      assert.equal(attempt.validated_json_sha256, sha256(retained.validated_json));
      assert.equal(later.paid_identity_summary.union_unique, 5);
      assert.equal(later.row_diagnostics[0].native_first_break,
        'fresh_native_receipt_authentication_required');
      assert.deepEqual(later.row_diagnostics[0].unresolved_native_attempts, []);
      assert.equal(later.canonical_report.retained_summary.graded, 29);
      assert.equal(later.canonical_report.summary_authority, 'not_authenticated_by_inventory');
      assert.equal(Object.hasOwn(later, 'graded'), false);
      assert.equal(JSON.stringify(later).includes(proposal.rationale), false);
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

for (const outcome of ['abstention', 'validator_failure']) {
  test(`inventory retains a paid ${outcome} before a later UNKNOWN revision`, (t) => {
    const w = world(t);
    const proposal = {
      decision: 'needs_more_evidence', confidence: 'low',
      rationale: 'The supplied evidence does not resolve the dispute.',
      ground_truth_refs: [],
      recommended_next_step: 'Inspect the missing acceptance evidence.',
      evidence_gaps: ['Independent acceptance evidence is unavailable.'],
      evidence_assessment: [{ claim: 'Acceptance criteria satisfied', status: 'insufficient',
        evidence_ref: null, reason: 'Independent acceptance evidence is unavailable.' }],
    };
    const result = {
      backend_run_id: 'earlier-native-paid',
      raw_output: outcome === 'abstention' ? JSON.stringify(proposal) : 'private invalid response',
      proposal: outcome === 'abstention' ? proposal : null,
      errors: outcome === 'abstention' ? [] : ['private validator rejection'],
    };
    // Complete the existing reservation through the producer, then cross the
    // invocation boundary on a new material revision without a raw response.
    // The fixture supplies receipts only; it never invokes a provider.
    const laterId = w.run(['-c', `
import json, sys
from pathlib import Path
import adjudicator_retro as retro

journal = retro.NativeAttemptJournal(Path(sys.argv[1]))
result = json.loads(sys.argv[4])
if result['proposal'] is not None:
    assert not retro.roles.ROLE_REGISTRY['adjudicator'].validate(result['proposal'])
journal.begin_invocation(sys.argv[2], {'run_id': result['backend_run_id']})
journal.persist_raw(sys.argv[2], {
    'run_id': result['backend_run_id'], 'output': result['raw_output'],
})
journal.validate(sys.argv[2], result)
assert not journal.reserve('original-a', 'unstarted', 'same-digest', sys.argv[3])['reserved']
later = journal.reserve('original-a', 'improved-evidence', 'new-digest', sys.argv[3], await_start=True)
assert later['reserved']
journal.begin_invocation(later['attempt_id'], {'run_id': 'later-unknown-paid'})
print(json.dumps(later['attempt_id']))
`, w.journal, w.ids[0], w.sourceHash, JSON.stringify(result)]);
    const before = w.snapshot();
    const report = w.run(w.inventoryArgs);
    const prior = report.native_journal.attempts.find((item) => item.attempt_id === w.ids[0]);
    const later = report.native_journal.attempts.find((item) => item.attempt_id === laterId);
    assert.equal(report.native_journal.attempts.length, 4);
    assert.equal(prior.state, 'validated');
    assert.equal(prior.backend_run_id, result.backend_run_id);
    assert.equal(prior.raw_response_sha256, sha256(result.raw_output));
    const saved = w.connection.prepare('SELECT validated_json FROM native_attempts WHERE attempt_id=?')
      .get(w.ids[0]);
    assert.deepEqual(JSON.parse(saved.validated_json), result);
    assert.equal(prior.validated_json_sha256, sha256(saved.validated_json));
    assert.equal(prior.matches_original_source, true);
    assert.equal(prior.in_original_cohort, true);
    assert.equal(prior.receipt_authority, 'not_authenticated_by_inventory');
    assert.equal(later.state, 'in_flight');
    assert.equal(later.backend_run_id, 'later-unknown-paid');
    assert.equal(later.raw_response_sha256, null);
    assert.equal(later.validated_json_sha256, null);
    assert.equal(later.matches_original_source, true);
    const diagnostic = report.row_diagnostics[0];
    assert.equal(diagnostic.retained_decision, 'needs_more_evidence');
    assert.equal(diagnostic.native_first_break, 'unresolved_native_attempt');
    assert.deepEqual(diagnostic.unresolved_native_attempts, [{
      attempt_id: laterId, state: 'in_flight', matches_original_source: true, outcome: 'UNKNOWN',
    }]);
    assert.deepEqual(report.original_paid_attempts.map((item) => item.backend_run_id),
      ['original-paid', 'earlier-paid']);
    assert.deepEqual(report.paid_identity_summary, {
      original_records: 2, original_unique: 2, native_records: 4, native_unique: 4,
      union_unique: 6, overlapping_backend_ids: [],
    });
    assert.equal(report.canonical_report.retained_summary.graded, 29);
    assert.equal(report.canonical_report.summary_authority, 'not_authenticated_by_inventory');
    assert.equal(report.dispatch_authorized, false);
    assert.equal('graded' in report, false);
    assert.equal(JSON.stringify(report).includes(result.raw_output), false);
    assert.equal(JSON.stringify(report).includes('private validator rejection'), false);
    assert.deepEqual(w.snapshot(), before);
    for (const name of ['absent-state', 'absent-runtime', 'absent-brain.sqlite']) {
      assert.equal(fs.existsSync(path.join(w.root, name)), false);
    }
  });
}

test('inventory CLI prints each mixed-cohort first break without rewriting decisions or paid history', (t) => {
  const rejection = 'recommended_next_step must not contain mutating execution instructions';
  function substantive(case_id) {
    return {
      case_id, target: 'fixture/repo#1', backend_run_id: `paid-${case_id}`,
      decision: 'reject_blocker', later_truth: 'PASS',
      packet: { ground_truth_evidence: {
        diff_patch: { text: '+private inspected change', revision: 'evaluated' },
        acceptance_criteria: [{ text: 'Inspect evidence', revision: 'issue-revision' }],
      } },
      proposal: {
        decision: 'reject_blocker', confidence: 'high',
        rationale: 'Private inspected evidence contradicts the blocker.',
        ground_truth_refs: ['git-path:proof.txt'],
        recommended_next_step: 'Inspect the proof before acting.', evidence_gaps: [],
        evidence_assessment: [{ claim: 'proof missing', status: 'contradicted',
          evidence_ref: 'git-path:proof.txt', reason: 'Proof contents supplied.' }],
      },
    };
  }
  const missing = { case_id: 'original-a', decision: 'needs_more_evidence',
    backend_run_id: 'original-paid',
    retry_attempts: [{ retry_attempts: [{ backend_run_id: 'earlier-paid',
      decision: null, cost_usd: null }] }] };
  const metadata = substantive('metadata');
  delete metadata.packet.ground_truth_evidence.diff_patch;
  metadata.errors = [rejection];
  const invalid = substantive('invalid-proposal');
  invalid.proposal.recommended_next_step = 'Merge this PR now';
  const discarded = substantive('discarded-proposal');
  delete discarded.proposal;
  discarded.decision = null;
  discarded.errors = [rejection];
  const noTruth = substantive('no-truth');
  noTruth.later_truth = null;
  const abstention = substantive('abstention');
  delete abstention.proposal;
  abstention.decision = 'needs_more_evidence';
  abstention.later_truth = null;
  const failed = substantive('backend-error');
  failed.errors = ['private provider failure', rejection];
  const mismatch = substantive('mismatch');
  mismatch.decision = 'uphold_blocker';
  const rows = [substantive('decided'), missing, metadata, invalid, discarded,
    noTruth, abstention, failed, mismatch];
  const w = world(t, rows);
  const publication = JSON.parse(fs.readFileSync(w.canonical, 'utf8'));
  // Neither a canonical claim nor a terminal binary receipt supplies independent truth.
  publication.rows[5].native_projection = { trusted: true, graded: 1, later_truth: 'PASS' };
  fs.writeFileSync(w.canonical, JSON.stringify(publication));
  const result = { backend_run_id: 'native-no-truth-paid', proposal: noTruth.proposal,
    raw_output: JSON.stringify(noTruth.proposal), errors: [] };
  const receiptId = w.run(['-c', `
import json, sys
from pathlib import Path
import adjudicator_retro as retro
result = json.loads(sys.argv[3])
assert not retro.roles.ROLE_REGISTRY['adjudicator'].validate(result['proposal'])
journal = retro.NativeAttemptJournal(Path(sys.argv[1]))
attempt = journal.reserve('no-truth', 'evaluated', 'case-digest', sys.argv[2], await_start=True)
journal.begin_invocation(attempt['attempt_id'], {'run_id': result['backend_run_id']})
journal.persist_raw(attempt['attempt_id'], {
    'run_id': result['backend_run_id'], 'output': result['raw_output'],
})
journal.validate(attempt['attempt_id'], result)
print(json.dumps(attempt['attempt_id']))
`, w.journal, w.sourceHash, JSON.stringify(result)]);
  const before = w.snapshot();
  const report = w.run(w.inventoryArgs);
  assert.deepEqual(report.original_report.case_ids, rows.map((row) => row.case_id));
  assert.deepEqual(report.row_diagnostics.map((row) => row.retained_first_break), [
    'fresh_native_authentication_required', 'saved_packet_unavailable',
    'saved_substantive_evidence_missing', 'saved_proposal_validator_failure',
    'saved_proposal_validator_failure', 'saved_independent_later_truth_unavailable',
    'saved_adjudicator_abstention', 'saved_attempt_error', 'saved_decision_proposal_mismatch',
  ]);
  assert.deepEqual(report.row_diagnostics.map((row) => row.retained_error_ref), [
    null, null, 'rows[2].errors[0]', null, 'rows[4].errors[0]', null, null,
    'rows[7].errors[0]', null,
  ]);
  assert.deepEqual(report.row_diagnostics.map((row) => row.retained_evidence_gap), [
    null, null, { ref: 'rows[2].packet.ground_truth_evidence.diff_patch',
      reason: 'missing_or_invalid_object' }, null, null, null, null, null, null,
  ], 'an earlier evidence gap remains specific despite a later validator rejection');
  report.row_diagnostics.forEach((diagnostic, index) => {
    assert.equal(diagnostic.source_row, `rows[${index}]`);
    assert.equal(diagnostic.case_id, rows[index].case_id);
    assert.equal(diagnostic.retained_decision, rows[index].decision);
    assert.equal(diagnostic.retained_later_truth, rows[index].later_truth ?? null);
    assert.equal(diagnostic.dispatch_authorized, false);
    assert.equal(Object.hasOwn(diagnostic, 'graded'), false);
    assert.match(diagnostic.evidence_scope, /not fresh collection or truth/);
    assert.equal(diagnostic.native_first_break, index === 1 ? 'unresolved_native_attempt'
      : index === 5 ? 'fresh_native_receipt_authentication_required'
        : 'fresh_native_collection_required');
    assert.deepEqual(diagnostic.unresolved_native_attempts, index === 1 ? [{
      attempt_id: w.ids[0], state: 'reserved', matches_original_source: true, outcome: 'UNKNOWN',
    }] : []);
  });
  assert.deepEqual(report.original_paid_attempts, rows.flatMap((row) => [
    { case_id: row.case_id, backend_run_id: row.backend_run_id },
    ...(row === missing ? [{ case_id: row.case_id, backend_run_id: 'earlier-paid' }] : []),
  ]));
  assert.equal(report.paid_identity_summary.original_records, 10);
  assert.equal(report.paid_identity_summary.union_unique, 13);
  const retained = report.native_journal.attempts.find((row) => row.attempt_id === receiptId);
  assert.equal(retained.state, 'validated');
  assert.equal(retained.receipt_authority, 'not_authenticated_by_inventory');
  assert.equal(report.canonical_report.retained_summary.graded, 29);
  assert.equal(report.canonical_report.summary_authority, 'not_authenticated_by_inventory');
  assert.equal(report.dispatch_authorized, false);
  assert.match(report.historical_observations, /^unavailable/);
  for (const payload of [result.raw_output, noTruth.proposal.rationale,
    'private provider failure', 'private inspected change']) {
    assert.equal(JSON.stringify(report).includes(payload), false);
  }
  assert.deepEqual(w.snapshot(), before);
  assert.deepEqual(JSON.parse(fs.readFileSync(w.original, 'utf8')).rows, rows);
  for (const name of ['absent-state', 'absent-runtime', 'absent-brain.sqlite']) {
    assert.equal(fs.existsSync(path.join(w.root, name)), false);
  }
});

test('inventory CLI locates incomplete evidence despite retained decisions and completeness claims', (t) => {
  const patch = { text: '+private inspected change', revision: 'evaluated' };
  const criterion = { text: 'Private acceptance criterion', revision: 'issue-revision' };
  const variants = [
    ['patch-absent', undefined, [criterion]],
    ['patch-null', null, [criterion]],
    ['patch-string', '+private inspected change', [criterion]],
    ['patch-empty', {}, [criterion]],
    ['patch-blank-text', { ...patch, text: ' \t\n' }, [criterion]],
    ['patch-nontext', { ...patch, text: ['private inspected change'] }, [criterion]],
    ['patch-no-revision', { text: patch.text }, [criterion]],
    ['patch-blank-revision', { ...patch, revision: ' \t' }, [criterion]],
    ['patch-nonstring-revision', { ...patch, revision: 42 }, [criterion]],
    ['patch-truncated', { ...patch, truncated: true }, [criterion]],
    ['criteria-absent', patch, undefined],
    ['criteria-null', patch, null],
    ['criteria-object', patch, criterion],
    ['criteria-empty', patch, []],
    ['criteria-null-member', patch, [criterion, null]],
    ['criteria-string-member', patch, ['Private acceptance criterion', criterion]],
    ['criteria-blank-text', patch, [criterion, { ...criterion, text: ' \n' }]],
    ['criteria-nontext', patch, [{ ...criterion, text: 42 }, criterion]],
    ['criteria-no-revision', patch, [criterion, { text: criterion.text }]],
    ['criteria-blank-revision', patch, [{ ...criterion, revision: ' \t' }, criterion]],
    ['criteria-nonstring-revision', patch, [criterion, { ...criterion, revision: 42 }]],
    ['patch-before-criteria', undefined, [null]],
    ['first-criterion-before-later', patch, [
      { ...criterion, text: '' }, { ...criterion, revision: '' },
    ]],
    ['complete-uphold', patch, [criterion]],
    ['complete-reject', patch, [criterion, { ...criterion, revision: 'other-issue-revision' }]],
  ];
  const expectedGaps = [
    ['diff_patch', 'missing_or_invalid_object'],
    ['diff_patch', 'missing_or_invalid_object'],
    ['diff_patch', 'missing_or_invalid_object'],
    ['diff_patch.text', 'missing_or_blank_string'],
    ['diff_patch.text', 'missing_or_blank_string'],
    ['diff_patch.text', 'missing_or_blank_string'],
    ['diff_patch.revision', 'missing_or_blank_string'],
    ['diff_patch.revision', 'missing_or_blank_string'],
    ['diff_patch.revision', 'missing_or_blank_string'],
    ['diff_patch.truncated', 'truncated_patch'],
    ['acceptance_criteria', 'missing_or_empty_list'],
    ['acceptance_criteria', 'missing_or_empty_list'],
    ['acceptance_criteria', 'missing_or_empty_list'],
    ['acceptance_criteria', 'missing_or_empty_list'],
    ['acceptance_criteria[1]', 'missing_or_invalid_object'],
    ['acceptance_criteria[0]', 'missing_or_invalid_object'],
    ['acceptance_criteria[1].text', 'missing_or_blank_string'],
    ['acceptance_criteria[0].text', 'missing_or_blank_string'],
    ['acceptance_criteria[1].revision', 'missing_or_blank_string'],
    ['acceptance_criteria[0].revision', 'missing_or_blank_string'],
    ['acceptance_criteria[1].revision', 'missing_or_blank_string'],
    ['diff_patch', 'missing_or_invalid_object'],
    ['acceptance_criteria[0].text', 'missing_or_blank_string'],
    null,
    null,
  ];
  const rows = variants.map(([name, diff_patch, acceptance_criteria], index) => ({
    // Include a real unresolved reservation on a row with a retained decision.
    case_id: index === 0 ? 'original-a' : name,
    target: 'fixture/repo#1', backend_run_id: `original-paid-${name}`,
    decision: index % 2 ? 'uphold_blocker' : 'reject_blocker',
    later_truth: index % 2 ? 'FAIL' : 'PASS',
    packet: { metadata_only: false, ground_truth_evidence: {
      diff_patch, acceptance_criteria,
      qualification: { trusted: true, complete: true },
    } },
    retry_attempts: [{ backend_run_id: `earlier-paid-${name}`, decision: 'needs_more_evidence' }],
  }));
  const w = world(t, rows);
  // JSON omits absent fields; compare against the actual retained representation.
  const saved = JSON.parse(fs.readFileSync(w.original, 'utf8'));
  const before = w.snapshot();
  const report = w.run(w.inventoryArgs);
  assert.equal(report.row_diagnostics.length, variants.length);
  assert.deepEqual(report.original_report.case_ids, rows.map((row) => row.case_id));
  report.row_diagnostics.forEach((diagnostic, index) => {
    const name = variants[index][0];
    assert.equal(diagnostic.retained_first_break, name.startsWith('complete-')
      ? 'fresh_native_authentication_required' : 'saved_substantive_evidence_missing', name);
    const gap = expectedGaps[index];
    assert.deepEqual(diagnostic.retained_evidence_gap, gap === null ? null : {
      ref: `rows[${index}].packet.ground_truth_evidence.${gap[0]}`, reason: gap[1],
    }, name);
    assert.equal(diagnostic.retained_decision, rows[index].decision, name);
    assert.equal(diagnostic.retained_later_truth, rows[index].later_truth, name);
    assert.equal(diagnostic.source_row, `rows[${index}]`, name);
    assert.equal(diagnostic.retained_error_ref, null, name);
    assert.equal(diagnostic.native_first_break, index === 0
      ? 'unresolved_native_attempt' : 'fresh_native_collection_required', name);
    assert.deepEqual(diagnostic.unresolved_native_attempts, index === 0 ? [{
      attempt_id: w.ids[0], state: 'reserved', matches_original_source: true, outcome: 'UNKNOWN',
    }] : [], name);
    assert.equal(diagnostic.dispatch_authorized, false, name);
    assert.equal(Object.hasOwn(diagnostic, 'graded'), false, name);
  });
  assert.deepEqual(report.original_paid_attempts, rows.flatMap((row) => [
    { case_id: row.case_id, backend_run_id: row.backend_run_id },
    { case_id: row.case_id, backend_run_id: row.retry_attempts[0].backend_run_id },
  ]));
  assert.equal(report.paid_identity_summary.original_records, rows.length * 2);
  assert.equal(report.paid_identity_summary.union_unique, rows.length * 2 + 2);
  assert.equal(report.dispatch_authorized, false);
  assert.equal(report.canonical_report.summary_authority, 'not_authenticated_by_inventory');
  for (const payload of [patch.text, criterion.text]) {
    assert.equal(JSON.stringify(report).includes(payload), false);
  }
  assert.deepEqual(w.snapshot(), before);
  assert.deepEqual(JSON.parse(fs.readFileSync(w.original, 'utf8')), saved);
  for (const name of ['absent-state', 'absent-runtime', 'absent-brain.sqlite']) {
    assert.equal(fs.existsSync(path.join(w.root, name)), false);
  }
});

test('inventory CLI diagnoses saved proposal contract failures before missing later truth', (t) => {
  const proposal = {
    decision: 'reject_blocker', confidence: 'high',
    rationale: 'Private supplied evidence contradicts the blocker.',
    ground_truth_refs: ['git-path:proof.txt'],
    recommended_next_step: 'Inspect the proof before acting.', evidence_gaps: [],
    evidence_assessment: [{ claim: 'Private proof missing', status: 'contradicted',
      evidence_ref: 'git-path:proof.txt', reason: 'Private proof contents supplied.' }],
  };
  const variants = [
    ['non-object', 'private unparsed response'],
    ['array', []],
    ['empty-object', {}],
    ['decision', { ...proposal, decision: ['reject_blocker'] }],
    ['confidence', { ...proposal, confidence: 'certain' }],
    ['rationale', { ...proposal, rationale: ' \t' }],
    ['next-step', { ...proposal, recommended_next_step: '' }],
    ['mutation', { ...proposal, recommended_next_step: 'If checks pass, merge this PR now.' }],
    ['terminal-key', { ...proposal, merge: true }],
    ['unexpected-key', { ...proposal, private_extra: 'private unrecognized field' }],
    ['refs-shape', { ...proposal, ground_truth_refs: 'git-path:proof.txt' }],
    ['refs-blank', { ...proposal, ground_truth_refs: ['git-path:proof.txt', ' '] }],
    ['binary-no-refs', { ...proposal, ground_truth_refs: [] }],
    ['gaps-shape', { ...proposal, evidence_gaps: {} }],
    ['assessment-empty', { ...proposal, evidence_assessment: [] }],
    ['assessment-member', { ...proposal, evidence_assessment: [null] }],
    ['assessment-status', { ...proposal, evidence_assessment: [
      { ...proposal.evidence_assessment[0], status: ['contradicted'] },
    ] }],
    ['assessment-ref', { ...proposal, evidence_assessment: [
      { ...proposal.evidence_assessment[0], evidence_ref: 42 },
    ] }],
    ['assessment-reason', { ...proposal, evidence_assessment: [
      { ...proposal.evidence_assessment[0], reason: '' },
    ] }],
    ['abstention-no-gap', { ...proposal, decision: 'needs_more_evidence' }],
  ];
  const invalidCount = variants.length;
  variants.push(
    ['valid-reject', proposal],
    ['valid-uphold', { ...proposal, decision: 'uphold_blocker' }],
    ['valid-abstention', { ...proposal, decision: 'needs_more_evidence',
      evidence_gaps: ['Private independent evidence is missing.'] }],
  );
  const rows = variants.map(([name, savedProposal], index) => ({
    case_id: index === 0 ? 'original-a' : name,
    target: 'fixture/repo#1', backend_run_id: `original-paid-${name}`,
    decision: typeof savedProposal?.decision === 'string' ? savedProposal.decision : 'reject_blocker',
    // Every row lacks independent truth. Proposal failures must be located first;
    // valid binary controls must reach the truth break instead of a validator break.
    later_truth: null, proposal: savedProposal,
    packet: { ground_truth_evidence: {
      diff_patch: { text: '+private inspected change', revision: 'evaluated' },
      acceptance_criteria: [{ text: 'Private criterion', revision: 'issue-revision' }],
    } },
    retry_attempts: [{ retry_attempts: [{ backend_run_id: `earlier-paid-${name}` }] }],
  }));
  const w = world(t, rows);
  const before = w.snapshot();
  const report = w.run(w.inventoryArgs);
  assert.equal(report.row_diagnostics.length, variants.length);
  assert.deepEqual(report.original_report.case_ids, rows.map((row) => row.case_id));
  report.row_diagnostics.forEach((diagnostic, index) => {
    const name = variants[index][0];
    assert.equal(diagnostic.retained_first_break, index < invalidCount
      ? 'saved_proposal_validator_failure' : name === 'valid-abstention'
        ? 'saved_adjudicator_abstention' : 'saved_independent_later_truth_unavailable', name);
    assert.equal(diagnostic.retained_decision, rows[index].decision, name);
    assert.equal(diagnostic.retained_later_truth, null, name);
    assert.equal(diagnostic.retained_error_ref, null, name);
    assert.equal(diagnostic.source_row, `rows[${index}]`, name);
    assert.equal(diagnostic.native_first_break, index === 0
      ? 'unresolved_native_attempt' : 'fresh_native_collection_required', name);
    assert.deepEqual(diagnostic.unresolved_native_attempts, index === 0 ? [{
      attempt_id: w.ids[0], state: 'reserved', matches_original_source: true, outcome: 'UNKNOWN',
    }] : [], name);
    assert.equal(diagnostic.dispatch_authorized, false, name);
    assert.equal(Object.hasOwn(diagnostic, 'graded'), false, name);
  });
  assert.deepEqual(report.original_paid_attempts, rows.flatMap((row) => [
    { case_id: row.case_id, backend_run_id: row.backend_run_id },
    { case_id: row.case_id, backend_run_id: row.retry_attempts[0].retry_attempts[0].backend_run_id },
  ]));
  assert.equal(report.paid_identity_summary.original_records, rows.length * 2);
  assert.equal(report.paid_identity_summary.union_unique, rows.length * 2 + 2);
  assert.equal(report.dispatch_authorized, false);
  assert.equal(report.canonical_report.summary_authority, 'not_authenticated_by_inventory');
  assert.match(report.historical_observations, /^unavailable/);
  assert.equal(JSON.stringify(report).includes('Private'), false);
  assert.equal(JSON.stringify(report).includes('private'), false);
  assert.deepEqual(w.snapshot(), before);
  assert.deepEqual(JSON.parse(fs.readFileSync(w.original, 'utf8')).rows, rows);
  for (const name of ['absent-state', 'absent-runtime', 'absent-brain.sqlite']) {
    assert.equal(fs.existsSync(path.join(w.root, name)), false);
  }
});

test('inventory CLI keeps recursive retry outcomes separate from original row diagnoses', (t) => {
  const decided = {
    target: 'fixture/repo#1', decision: 'reject_blocker', later_truth: 'PASS',
    packet: { ground_truth_evidence: {
      diff_patch: { text: '+private original patch', revision: 'evaluated' },
      acceptance_criteria: [{ text: 'Private original criterion', revision: 'issue-revision' }],
    } },
    proposal: {
      decision: 'reject_blocker', confidence: 'high',
      rationale: 'Private original evidence contradicts the blocker.',
      ground_truth_refs: ['git-path:proof.txt'],
      recommended_next_step: 'Inspect the proof before acting.', evidence_gaps: [],
      evidence_assessment: [{ claim: 'Private proof missing', status: 'contradicted',
        evidence_ref: 'git-path:proof.txt', reason: 'Private proof supplied.' }],
    },
  };
  const failed = { decision: null, later_truth: null, errors: ['Private retry failure'] };
  const rows = [
    { ...failed, case_id: 'original-a', target: decided.target, backend_run_id: 'original-failed',
      // Even a nested complete result cannot supply the original's missing packet.
      retry_attempts: [{ ...failed, backend_run_id: 'retry-failed', retry_attempts: [
        { ...decided, backend_run_id: 'nested-decided' },
      ] }] },
    { ...decided, case_id: 'original-decided', backend_run_id: 'original-decided-paid',
      // A later failed attempt cannot erase an original decision or its truth label.
      retry_attempts: [{ ...failed, backend_run_id: 'later-failed', retry_attempts: [
        { ...failed, backend_run_id: 'nested-failed' },
      ] }] },
  ];
  const w = world(t, rows);
  const before = w.snapshot();
  const report = w.run(w.inventoryArgs);
  assert.deepEqual(report.original_report.case_ids, ['original-a', 'original-decided']);
  assert.equal(report.row_diagnostics.length, 2, 'retry attempts are not additional original cases');
  assert.deepEqual(report.row_diagnostics.map((item) => ({
    case_id: item.case_id, target: item.target, source_row: item.source_row,
    decision: item.retained_decision, truth: item.retained_later_truth,
    first_break: item.retained_first_break, error_ref: item.retained_error_ref,
  })), [
    { case_id: 'original-a', target: decided.target, source_row: 'rows[0]',
      decision: null, truth: null, first_break: 'saved_packet_unavailable',
      error_ref: 'rows[0].errors[0]' },
    { case_id: 'original-decided', target: decided.target, source_row: 'rows[1]',
      decision: 'reject_blocker', truth: 'PASS',
      first_break: 'fresh_native_authentication_required', error_ref: null },
  ]);
  assert.deepEqual(report.original_paid_attempts, [
    { case_id: 'original-a', backend_run_id: 'original-failed' },
    { case_id: 'original-a', backend_run_id: 'retry-failed' },
    { case_id: 'original-a', backend_run_id: 'nested-decided' },
    { case_id: 'original-decided', backend_run_id: 'original-decided-paid' },
    { case_id: 'original-decided', backend_run_id: 'later-failed' },
    { case_id: 'original-decided', backend_run_id: 'nested-failed' },
  ]);
  assert.deepEqual(report.paid_identity_summary, {
    original_records: 6, original_unique: 6, native_records: 2, native_unique: 2,
    union_unique: 8, overlapping_backend_ids: [],
  });
  assert.equal(report.row_diagnostics[0].native_first_break, 'unresolved_native_attempt');
  assert.deepEqual(report.row_diagnostics[0].unresolved_native_attempts, [{
    attempt_id: w.ids[0], state: 'reserved', matches_original_source: true, outcome: 'UNKNOWN',
  }]);
  assert.equal(report.row_diagnostics[1].native_first_break, 'fresh_native_collection_required');
  assert.deepEqual(report.row_diagnostics[1].unresolved_native_attempts, []);
  for (const diagnostic of report.row_diagnostics) {
    assert.equal(diagnostic.dispatch_authorized, false);
    assert.equal(Object.hasOwn(diagnostic, 'graded'), false);
  }
  assert.equal(report.canonical_report.summary_authority, 'not_authenticated_by_inventory');
  assert.equal(report.dispatch_authorized, false);
  assert.match(report.historical_observations, /^unavailable/);
  assert.equal(JSON.stringify(report).includes('Private'), false);
  assert.equal(JSON.stringify(report).includes('private'), false);
  assert.deepEqual(w.snapshot(), before);
  assert.deepEqual(JSON.parse(fs.readFileSync(w.original, 'utf8')).rows, rows);
  for (const name of ['absent-state', 'absent-runtime', 'absent-brain.sqlite']) {
    assert.equal(fs.existsSync(path.join(w.root, name)), false);
  }
});

for (const foreign of [false, true]) {
  test(`inventory CLI retains first breaks across a ${foreign ? 'foreign' : 'matching'} receipt lifecycle`, (t) => {
    const row = {
      case_id: 'original-a', target: 'fixture/repo#1',
      decision: 'reject_blocker', later_truth: null, backend_run_id: 'original-paid',
      packet: { ground_truth_evidence: {
        diff_patch: { text: '+private retained patch', revision: 'evaluated' },
        acceptance_criteria: [{ text: 'Private criterion', revision: 'issue-revision' }],
      } },
      retry_attempts: [{ backend_run_id: 'earlier-paid' }],
    };
    const w = world(t, [row]);
    // SQLite can create an empty WAL on a read after producer cleanup has
    // checkpointed. Compare persisted bytes; absent and empty WALs carry none.
    const snapshot = () => [w.original, w.canonical, w.journal, `${w.journal}-wal`]
      .map((file) => [file, sha256(fs.existsSync(file) ? fs.readFileSync(file) : Buffer.alloc(0))]);
    const source = foreign ? 'foreign-source' : w.sourceHash;
    const attemptId = w.run(['-c', `
import json, sys
from pathlib import Path
from adjudicator_retro import NativeAttemptJournal
journal = NativeAttemptJournal(Path(sys.argv[1]))
assert journal.release_unstarted(sys.argv[2])
attempt = journal.reserve('original-a', 'evaluated', 'digest', sys.argv[3], await_start=True)
assert attempt['reserved']
print(json.dumps(attempt['attempt_id']))
`, w.journal, w.ids[0], source]);
    const proposal = {
      decision: 'reject_blocker', confidence: 'high',
      rationale: 'Private provider rationale.', ground_truth_refs: ['git-path:proof.txt'],
      recommended_next_step: 'If checks pass, merge this PR now.', evidence_gaps: [],
      evidence_assessment: [{ claim: 'Private claim', status: 'contradicted',
        evidence_ref: 'git-path:proof.txt', reason: 'Private evidence.' }],
    };
    const raw = JSON.stringify(proposal);
    for (const state of ['reserved', 'in_flight', 'raw_persisted', 'validated']) {
      if (state !== 'reserved') {
        w.run(['-c', `
import json, sys
from pathlib import Path
import adjudicator_retro as retro
journal = retro.NativeAttemptJournal(Path(sys.argv[1]))
attempt_id, state, raw = sys.argv[2:5]
if state == 'in_flight':
    journal.begin_invocation(attempt_id, {'run_id': 'original-paid'})
elif state == 'raw_persisted':
    journal.persist_raw(attempt_id, {'run_id': 'original-paid', 'output': raw})
else:
    errors = retro.roles.ROLE_REGISTRY['adjudicator'].validate(json.loads(raw))
    assert errors == ['recommended_next_step must not contain mutating execution instructions']
    journal.validate(attempt_id, {
        'backend_run_id': 'original-paid', 'raw_output': raw, 'proposal': None, 'errors': errors,
    })
print(json.dumps(True))
`, w.journal, attemptId, state, raw]);
      }
      const before = snapshot();
      const report = w.run(w.inventoryArgs);
      const diagnostic = report.row_diagnostics[0];
      const receipt = report.native_journal.attempts.find((item) => item.attempt_id === attemptId);
      assert.equal(receipt.state, state);
      assert.equal(receipt.matches_original_source, !foreign);
      assert.equal(receipt.in_original_cohort, true);
      assert.equal(receipt.backend_run_id, state === 'reserved' ? null : 'original-paid');
      assert.equal(receipt.raw_response_sha256,
        ['raw_persisted', 'validated'].includes(state) ? sha256(raw) : null);
      assert.equal(receipt.validated_json_sha256 !== null, state === 'validated');
      assert.equal(receipt.receipt_authority, 'not_authenticated_by_inventory');
      assert.equal(diagnostic.retained_decision, 'reject_blocker');
      assert.equal(diagnostic.retained_later_truth, null);
      assert.equal(diagnostic.retained_first_break, 'saved_independent_later_truth_unavailable');
      assert.equal(diagnostic.native_first_break, state !== 'validated'
        ? 'unresolved_native_attempt' : foreign
          ? 'foreign_source_native_attempt_requires_review' : 'fresh_native_receipt_authentication_required');
      assert.deepEqual(diagnostic.unresolved_native_attempts, state === 'validated' ? [] : [{
        attempt_id: attemptId, state, matches_original_source: !foreign, outcome: 'UNKNOWN',
      }]);
      assert.deepEqual(report.original_paid_attempts, [
        { case_id: 'original-a', backend_run_id: 'original-paid' },
        { case_id: 'original-a', backend_run_id: 'earlier-paid' },
      ]);
      assert.deepEqual(report.paid_identity_summary, {
        original_records: 2, original_unique: 2,
        native_records: state === 'reserved' ? 2 : 3,
        native_unique: state === 'reserved' ? 2 : 3,
        union_unique: 4, overlapping_backend_ids: state === 'reserved' ? [] : ['original-paid'],
      });
      assert.equal(report.canonical_report.retained_summary.graded, 29);
      assert.equal(report.canonical_report.summary_authority, 'not_authenticated_by_inventory');
      assert.equal(diagnostic.dispatch_authorized, false);
      assert.equal(report.dispatch_authorized, false);
      assert.equal(Object.hasOwn(diagnostic, 'graded'), false);
      assert.match(report.historical_observations, /^unavailable/);
      assert.equal(JSON.stringify(report).includes('Private'), false);
      assert.equal(JSON.stringify(report).includes('private retained patch'), false);
      assert.equal(JSON.stringify(report).includes(proposal.recommended_next_step), false);
      assert.deepEqual(snapshot(), before);
    }
    assert.deepEqual(JSON.parse(fs.readFileSync(w.original, 'utf8')).rows, [row]);
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

for (const change of ['atomic_replacement', 'symlink_switch']) {
  test(`native inventory refuses journal ${change} despite identical retained receipts`, (t) => {
    const w = world(t);
    const replacement = path.join(w.root, 'replacement.sqlite');
    fs.copyFileSync(w.journal, replacement);
    let selected = w.journal;
    if (change === 'symlink_switch') {
      // The main file alone contains no receipts: retain the committed WAL
      // when preparing the second, independently addressable journal.
      fs.copyFileSync(`${w.journal}-wal`, `${replacement}-wal`);
      selected = path.join(w.root, 'selected.sqlite');
      fs.symlinkSync(w.journal, selected);
    }
    const args = [...w.inventoryArgs];
    args[args.indexOf('--native-journal') + 1] = selected;
    const initialPath = fs.realpathSync(selected);
    const initialInode = fs.statSync(selected).ino;
    const before = w.snapshot();
    const result = w.execute(['-c', `
import sys
from pathlib import Path
import adjudicator_retro as retro

selected, replacement = map(Path, sys.argv[1:3])
change = sys.argv[3]
observe = retro._inventory_code
switched = False

def observe_then_switch(root):
    global switched
    result = observe(root)
    if not switched:
        switched = True
        if change == 'symlink_switch':
            temporary = selected.with_name(selected.name + '.next')
            temporary.symlink_to(replacement)
            temporary.replace(selected)
        else:
            replacement.replace(selected)
    return result

# Both record reads still use SQLite's original open file. An equal receipt
# list cannot establish that the selected path refers to that same journal.
retro._inventory_code = observe_then_switch
sys.argv = ['adjudicator_retro.py', *sys.argv[4:]]
raise SystemExit(retro.main())
`, selected, replacement, change, ...args.slice(1)]);
    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, /native journal location changed during inventory/);
    assert.equal(result.stdout, '', 'a switched journal must not emit a partial inventory');
    assert.notEqual(fs.statSync(selected).ino, initialInode);
    assert.equal(fs.realpathSync(selected) === initialPath, change === 'atomic_replacement');
    assert.deepEqual(w.snapshot(), before, 'reports and receipt bytes must remain unchanged');

    // A stable observation of the replacement must still see all WAL receipts,
    // including UNKNOWN attempts and the separately attributable foreign row.
    const later = w.run(args);
    assert.equal(later.native_journal.path, fs.realpathSync(selected));
    assert.equal(later.original_report.sha256, w.sourceHash);
    assert.deepEqual(later.original_report.case_ids, ['original-a']);
    assert.deepEqual(later.original_paid_attempts.map((item) => item.backend_run_id),
      ['original-paid', 'earlier-paid']);
    const attempts = new Map(later.native_journal.attempts.map((item) => [item.attempt_id, item]));
    assert.deepEqual(w.ids.map((id) => attempts.get(id).state),
      ['reserved', 'in_flight', 'raw_persisted']);
    assert.equal(attempts.get(w.ids[2]).matches_original_source, false);
    assert.equal(attempts.get(w.ids[2]).raw_response_sha256, sha256(w.raw));
    assert.equal(later.row_diagnostics[0].unresolved_native_attempts[0].outcome, 'UNKNOWN');
    assert.equal(later.paid_identity_summary.union_unique, 4);
    assert.equal(later.canonical_report.summary_authority, 'not_authenticated_by_inventory');
    assert.equal(later.dispatch_authorized, false);
    assert.match(later.historical_observations, /^unavailable/);
    assert.equal(JSON.stringify(later).includes(w.raw), false);
    assert.deepEqual(w.snapshot(), before);
  });
}

for (const input of ['original', 'canonical']) {
  for (const change of ['atomic_replacement', 'symlink_switch']) {
    for (const boundary of ['code_observation', 'final_report_read']) {
      test(`native inventory refuses ${input} ${change} at ${boundary} with identical bytes`, (t) => {
        const w = world(t);
        const selected = w[input];
        const retained = `${selected}.retained`;
        const replacement = `${selected}.replacement`;
        fs.copyFileSync(selected, replacement);
        if (change === 'symlink_switch') {
          fs.renameSync(selected, retained);
          fs.symlinkSync(retained, selected);
        }
        const initialPath = fs.realpathSync(selected);
        const initialInode = fs.statSync(selected).ino;
        const before = w.snapshot();
        // Switch after the first journal read, or after the final report bytes
        // have been read but before its location recheck. Hash equality alone
        // cannot prove these observations still refer to the selected reports.
        const result = w.execute(['-c', `
import sys
from pathlib import Path
import adjudicator_retro as retro

selected, replacement = map(Path, sys.argv[1:3])
change, boundary = sys.argv[3:5]
observe = retro._inventory_code
read_bytes = Path.read_bytes
armed = False
switched = False

def switch():
    global switched
    switched = True
    if change == 'symlink_switch':
        temporary = selected.with_name(selected.name + '.next')
        temporary.symlink_to(replacement)
        temporary.replace(selected)
    else:
        replacement.replace(selected)

def observe_after_read(root):
    global armed
    result = observe(root)
    armed = True
    if boundary == 'code_observation' and not switched:
        switch()
    return result

def read_then_switch(path):
    data = read_bytes(path)
    if armed and not switched and path == selected:
        switch()
    return data

retro._inventory_code = observe_after_read
Path.read_bytes = read_then_switch
sys.argv = ['adjudicator_retro.py', *sys.argv[5:]]
raise SystemExit(retro.main())
`, selected, replacement, change, boundary, ...w.inventoryArgs.slice(1)]);
        assert.equal(result.status, 2, result.stderr);
        assert.match(result.stderr, /native report location changed during inventory/);
        assert.equal(result.stdout, '', 'a switched report must not emit a partial inventory');
        assert.notEqual(fs.statSync(selected).ino, initialInode);
        assert.equal(fs.realpathSync(selected) === initialPath, change === 'atomic_replacement');
        assert.deepEqual(w.snapshot(), before, 'report and receipt bytes must remain unchanged');

        // Once the selected locations are stable, a new observation retains
        // the original cohort, recursive paid history and unresolved receipts.
        const later = w.run(w.inventoryArgs);
        assert.equal(later.original_report.sha256, w.sourceHash);
        assert.deepEqual(later.original_report.case_ids, ['original-a']);
        assert.deepEqual(later.original_paid_attempts.map((item) => item.backend_run_id),
          ['original-paid', 'earlier-paid']);
        assert.equal(later.native_journal.attempts.length, 3);
        assert.equal(later.row_diagnostics[0].unresolved_native_attempts[0].outcome, 'UNKNOWN');
        assert.equal(later.paid_identity_summary.union_unique, 4);
        assert.equal(later.canonical_report.summary_authority, 'not_authenticated_by_inventory');
        assert.equal(later.dispatch_authorized, false);
        assert.match(later.historical_observations, /^unavailable/);
        assert.equal(JSON.stringify(later).includes(w.raw), false);
        assert.deepEqual(w.snapshot(), before);
      });
    }
  }
}
