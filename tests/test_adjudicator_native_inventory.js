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

function world(t, rows = [{ case_id: 'original-a', decision: 'needs_more_evidence',
  backend_run_id: 'original-paid',
  retry_attempts: [{ retry_attempts: [{ backend_run_id: 'earlier-paid' }] }] }]) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'native-inventory-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  // URI metacharacters must remain part of the selected journal's filename.
  const journal = path.join(root, 'native #1?.sqlite');
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
