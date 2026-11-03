'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { test } = require('node:test');
const { inventory } = require('../scripts/export_adjudicator_inventory');

const repo = path.resolve(__dirname, '..');
const script = path.join(repo, 'scripts/export_adjudicator_inventory.js');
const now = 2000000000;
const cutoff = now - 90 * 86400;

test('owner exclusions are accounted for without reading excluded outcome evidence', (t) => {
  const { root, db } = world(t);
  const connection = new DatabaseSync(db);
  connection.exec(`
    INSERT INTO runs VALUES
      ('excluded', 'StRaNsKe/CoLlAb-AdMiN#1', ${now}),
      ('allowed', 'stranske/Orchestrator#1', ${now}),
      ('lookalike', 'stranske/Collab-Admin-archive#1', ${now});
    ALTER TABLE runs RENAME TO run_rows;
    CREATE VIEW runs AS SELECT run_id,target,
      CASE WHEN run_id='excluded' THEN json_extract('unreadable', '$')
           ELSE ts END AS ts FROM run_rows;
    ALTER TABLE outcomes RENAME TO outcome_rows;
    INSERT INTO outcome_rows VALUES
      ('excluded','NON_PASS','PASS',1,'durable',${now}),
      ('allowed','NON_PASS','PASS',1,'durable',${now}),
      ('lookalike','NON_PASS','PASS',1,'durable',${now});
    CREATE VIEW outcomes AS SELECT run_id,
      CASE WHEN run_id='excluded' THEN json_extract('unreadable', '$')
           ELSE verifier_verdict END AS verifier_verdict,
      adjudicated_verdict,merged,durability,durability_checked_ts FROM outcome_rows;
  `);
  assert.throws(() => connection.prepare(
    "SELECT verifier_verdict FROM outcomes WHERE run_id='excluded'").get(), /malformed JSON/,
  'the fixture must fail if excluded evidence is evaluated');
  assert.throws(() => connection.prepare(
    "SELECT ts FROM runs WHERE run_id='excluded'").get(), /malformed JSON/,
  'excluded timestamps must not be evaluated either');
  connection.close();
  const requested = ['excluded', 'allowed', 'lookalike'].map((run_id) => ({
    run_id, target: run_id === 'excluded' ? 'StRaNsKe/CoLlAb-AdMiN#1' :
      run_id === 'allowed' ? 'stranske/Orchestrator#1' : 'stranske/Collab-Admin-archive#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS',
  }));
  const manifest = manifestFile(root, requested, 2);
  const before = fs.readFileSync(db);
  const manifestBefore = fs.readFileSync(manifest);
  const report = inventory({ db, observedAt: now, expectedCount: 3, manifest });
  assert.equal(report.status, 'available');
  assert.deepEqual(report.owner_excluded_repositories, ['stranske/Collab-Admin']);
  assert.equal(report.population_count, 3);
  assert.equal(report.included_count, 2);
  assert.equal(report.excluded_count, 1);
  assert.equal(report.count_delta, -1);
  assert.deepEqual(report.exclusions, [{ run_id: 'excluded', target: requested[0].target,
    reasons: ['owner_excluded_repository'] }]);
  assert.deepEqual(report.manifest_reconciliation.cases, requested.map((item, i) => ({
    ...item, status: i === 0 ? 'excluded' : 'included',
    reasons: i === 0 ? ['owner_excluded_repository'] : [],
  })));
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.equal(report.requested_cohort_verified, false);
  assert.ok(report.evidence_gaps.some((gap) => gap.kind === 'inventory_count_discrepancy'));
  assert.ok(report.evidence_gaps.some((gap) => gap.kind === 'requested_case_evidence_gap' &&
    gap.run_id === 'excluded' && gap.reasons.includes('owner_excluded_repository')));
  fs.writeFileSync(path.join(root, 'stdout'), '');
  fs.writeFileSync(path.join(root, 'stderr'), '');
  const files = fs.readdirSync(root);
  const result = execute(root, process.execPath, [script, '--db', db,
    '--observed-at', String(now), '--expected-count', '3', '--manifest', manifest]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), report);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readFileSync(manifest), manifestBefore);
  assert.deepEqual(fs.readdirSync(root), files, 'no reports or SQLite sidecars created');
});

test('requested owner exclusions remain explicit with missing or contradictory inventory', (t) => {
  const { root, db } = world(t);
  seed(db, [['reused-run', now, 'NON_PASS', 'PASS']]);
  const requested = ['reused-run', 'absent'].map((run_id) => ({
    run_id, target: 'stranske/Collab-Admin#7',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS',
  }));
  for (const version of [1, 2]) {
    const manifest = manifestFile(root, requested, version);
    for (const candidate of [db, path.join(root, 'unavailable.db')]) {
      const report = inventory({ db: candidate, observedAt: now, expectedCount: 2, manifest });
      assert.deepEqual(report.manifest_reconciliation.cases, requested.map((item) => ({
        run_id: item.run_id, target: item.target,
        ...(version === 2 ? { verifier_verdict: item.verifier_verdict,
          adjudicated_verdict: item.adjudicated_verdict } : {}),
        status: 'excluded', reasons: ['owner_excluded_repository'],
      })));
      assert.equal(report.manifest_reconciliation.exact_match, false);
      assert.equal(report.requested_cohort_verified, false);
      assert.equal(fs.existsSync(path.join(root, 'unavailable.db')), false);
    }
  }
});

function world(t, schema = true) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-inventory-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const db = path.join(root, 'brain.db');
  const connection = new DatabaseSync(db);
  if (schema) connection.exec(`
    CREATE TABLE runs (run_id TEXT PRIMARY KEY, target TEXT, ts INTEGER);
    CREATE TABLE outcomes (run_id TEXT PRIMARY KEY, verifier_verdict TEXT,
      adjudicated_verdict TEXT, merged INTEGER, durability TEXT,
      durability_checked_ts INTEGER);
  `);
  connection.close();
  return { root, db };
}

test('identity accounting keeps unsupplied slots separate from missing, excluded and mismatched cases', (t) => {
  const { root, db } = world(t);
  seed(db, [
    ['included', now, 'NON_PASS', 'PASS'],
    ['mismatch', now, 'NON_PASS', 'PASS'],
    ['no-outcome', now],
  ]);
  const requested = [
    { run_id: 'included', target: 'owner/repo#1' },
    { run_id: 'mismatch', target: 'owner/repo#2' },
    { run_id: 'no-outcome', target: 'owner/repo#1' },
    { run_id: 'absent', target: 'owner/repo#1' },
    { run_id: 'owner-excluded', target: 'stranske/Collab-Admin#1' },
  ].map((row) => ({ ...row, verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' }));
  const manifest = manifestFile(root, requested, 2);
  const before = fs.readFileSync(db);
  const report = inventory({ db, observedAt: now, expectedCount: 71, manifest });
  assert.deepEqual(report.requested_identity_accounting, {
    scope: 'supplied_manifest_only', expected_count: 71, supplied_case_count: 5,
    unsupplied_requested_slots: 66, excess_supplied_cases: 0,
    by_status: { included: 1, excluded: 1, missing: 1, unavailable: 1, identity_mismatch: 1 },
  });
  assert.equal(report.manifest_reconciliation.count_delta, -66);
  assert.equal(report.requested_cohort_verified, false);
  const excess = inventory({ db, observedAt: now, expectedCount: 4, manifest });
  assert.equal(excess.requested_identity_accounting.unsupplied_requested_slots, 0);
  assert.equal(excess.requested_identity_accounting.excess_supplied_cases, 1);
  assert.equal(excess.manifest_reconciliation.count_delta, 1);
  assert.equal(excess.requested_cohort_verified, false);
  const result = execute(root, process.execPath, [script, '--db', db,
    '--observed-at', String(now), '--manifest', manifest]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), report);
  assert.deepEqual(fs.readFileSync(db), before);
});

test('an equal-sized later inventory supplies no original identities without a manifest', (t) => {
  const { root, db } = world(t);
  seed(db, Array.from({ length: 71 }, (_, n) => [`later-${n}`, now, 'NON_PASS', 'PASS']));
  const withoutManifest = inventory({ db, observedAt: now });
  assert.equal(withoutManifest.count_delta, 0);
  assert.deepEqual(withoutManifest.requested_identity_accounting, {
    scope: 'supplied_manifest_only', expected_count: 71, supplied_case_count: 0,
    unsupplied_requested_slots: 71, excess_supplied_cases: 0, by_status: null,
  });
  const empty = inventory({ db, observedAt: now, manifest: manifestFile(root, [], 2) });
  assert.equal(empty.requested_identity_accounting.unsupplied_requested_slots, 71);
  assert.equal(Object.values(empty.requested_identity_accounting.by_status).reduce((a, b) => a + b), 0);
  assert.equal(empty.manifest_reconciliation.unexpected_cases.length, 71);
  assert.equal(empty.requested_cohort_verified, false);
  assert.equal(empty.manifest_reconciliation.exact_match, false);
});

for (const version of [1, 2]) {
  test(`schema ${version} accounts for unsupplied slots even with unavailable inventory`, (t) => {
    const { root, db } = world(t);
    fs.unlinkSync(db);
    const manifest = manifestFile(root, [
      { run_id: 'unknown', target: 'owner/repo#1', verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' },
      { run_id: 'excluded', target: 'stranske/Collab-Admin#1', verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' },
    ], version);
    const report = inventory({ db, observedAt: now, manifest });
    assert.equal(report.status, 'unavailable');
    assert.equal(report.included_count, null);
    assert.deepEqual(report.requested_identity_accounting, {
      scope: 'supplied_manifest_only', expected_count: 71, supplied_case_count: 2,
      unsupplied_requested_slots: 69, excess_supplied_cases: 0,
      by_status: { included: 0, excluded: 1, missing: 0, unavailable: 1, identity_mismatch: 0 },
    });
    const result = execute(root, process.execPath, [script, '--db', db,
      '--observed-at', String(now), '--manifest', manifest]);
    assert.equal(result.status, 2, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), report);
    assert.equal(fs.existsSync(db), false);
  });
}

function seed(db, rows) {
  const connection = new DatabaseSync(db);
  try {
    const run = connection.prepare('INSERT INTO runs VALUES (?, ?, ?)');
    const outcome = connection.prepare('INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?)');
    for (const [id, ts, verifier, adjudicated, merged = 1] of rows) {
      run.run(id, 'owner/repo#1', ts);
      if (verifier !== undefined) outcome.run(id, verifier, adjudicated, merged, 'durable', now);
    }
  } finally {
    connection.close();
  }
}

function execute(root, binary, args) {
  const out = path.join(root, 'stdout');
  const err = path.join(root, 'stderr');
  const stdout = fs.openSync(out, 'w');
  const stderr = fs.openSync(err, 'w');
  let result;
  try {
    result = spawnSync(binary, args, {
      cwd: repo, timeout: 10000,
      env: { ...process.env, PYTHONPATH: path.join(repo, 'src'), PYTHONDONTWRITEBYTECODE: '1' },
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

test('inventory exports every synthetic dispute and every exclusion without writes', (t) => {
  const { root, db } = world(t);
  const eligible = Array.from({ length: 71 }, (_, i) => [
    `dispute-${String(i).padStart(2, '0')}`, i === 0 ? cutoff : now,
    i < 57 ? 'NON_PASS' : i < 66 ? 'FAIL_HOLLOW' : 'PASS',
    i < 57 ? 'PASS' : 'FAIL', i < 57 ? 1 : 0,
  ]).reverse();
  seed(db, [...eligible,
    ['old', cutoff - 1, 'NON_PASS', 'PASS'],
    ['agree', now, 'PASS', 'PASS'],
    ['no-verifier', now, null, 'PASS'],
    ['no-adjudication', now, 'NON_PASS', null],
    ['both-missing', now, null, null],
    ['no-outcome', now],
    ['old-agreement', cutoff - 1, 'PASS', 'PASS'],
    ['no-timestamp', null, 'NON_PASS', 'PASS'],
  ]);
  const before = fs.readFileSync(db);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now });
  assert.equal(report.status, 'available');
  assert.equal(report.population_count, 79);
  assert.equal(report.included_count, 71);
  assert.equal(report.excluded_count, 8);
  assert.equal(report.orphaned_outcome_count, 0);
  assert.equal(report.count_delta, 0);
  assert.equal(report.cutoff_inclusive, cutoff);
  assert.deepEqual(report.cases.map((row) => row.run_id), [
    ...eligible.slice(0, -1).reverse().map((row) => row[0]), 'dispute-00',
  ]);
  assert.equal(new Set(report.cases.map((row) => row.run_id)).size, 71);
  assert.equal(new Set(report.cases.map((row) => row.target)).size, 1);
  assert.deepEqual(Object.fromEntries(report.exclusions.map((row) => [row.run_id, row.reasons])), {
    agree: ['agreeing_verdicts'],
    'no-verifier': ['missing_verifier_verdict'],
    'no-adjudication': ['missing_adjudicated_verdict'],
    'both-missing': ['missing_verifier_verdict', 'missing_adjudicated_verdict'],
    'no-outcome': ['missing_outcome'],
    old: ['outside_window'],
    'old-agreement': ['outside_window', 'agreeing_verdicts'],
    'no-timestamp': ['missing_timestamp'],
  });
  assert.equal(report.requested_cohort_verified, false, 'matching counts cannot prove case identities');
  assert.deepEqual(report.evidence_gaps.map((gap) => gap.kind), ['requested_cohort_unverified']);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readdirSync(root), files, 'no report, role store or SQLite sidecar created');
  // Bind the export to the production Python selection, including row ordering
  // and all fields needed to derive its existing case identities.
  const reference = execute(root, process.env.PYTHON || 'python3', ['-c', `
import json
from pathlib import Path
import adjudicator_retro as retro
print(json.dumps(retro.disputes(db=Path(${JSON.stringify(db)}), now=${now})))
`]);
  assert.equal(reference.status, 0, reference.stderr);
  assert.deepEqual(report.cases, JSON.parse(reference.stdout));
});

test('inventory preserves the fixed cutoff and reports a count discrepancy', (t) => {
  const { db } = world(t);
  seed(db, [['boundary', cutoff, 'NON_PASS', 'PASS'], ['future', now + 1, 'PASS', 'FAIL']]);
  const report = inventory({ db, observedAt: now });
  assert.equal(report.included_count, 2);
  assert.equal(report.count_delta, -69);
  assert.equal(report.upper_time_bound, null);
  assert.ok(report.evidence_gaps.some((gap) => gap.kind === 'inventory_count_discrepancy'));
  const moved = inventory({ db, observedAt: now + 1, expectedCount: 1 });
  assert.deepEqual(moved.cases.map((row) => row.run_id), ['future']);
  assert.deepEqual(moved.exclusions[0].reasons, ['outside_window']);
  assert.deepEqual(inventory({ db, observedAt: now }), report);
  assert.equal(inventory({ db, observedAt: now, expectedCount: 1 }).count_delta, 1);
});

for (const state of ['missing', 'schema-less', 'empty', 'incomplete-schema']) {
  test(`inventory distinguishes ${state} evidence from a complete cohort`, (t) => {
    const { root, db } = world(t, state !== 'schema-less');
    if (state === 'missing') fs.unlinkSync(db);
    if (state === 'incomplete-schema') {
      const connection = new DatabaseSync(db);
      connection.exec('DROP TABLE outcomes');
      connection.close();
    }
    const before = fs.existsSync(db) ? fs.readFileSync(db) : null;
    const files = fs.readdirSync(root);
    const report = inventory({ db, observedAt: now });
    if (state === 'empty') {
      assert.equal(report.status, 'available');
      assert.deepEqual(report.cases, []);
      assert.equal(report.included_count, 0);
      assert.equal(report.orphaned_outcome_count, 0);
      assert.equal(report.count_delta, -71);
    } else {
      assert.equal(report.status, 'unavailable');
      for (const key of ['cases', 'exclusions', 'included_count', 'excluded_count',
        'population_count', 'count_delta', 'orphaned_outcome_count']) assert.equal(report[key], null, key);
      assert.ok(report.evidence_gaps.some((gap) => gap.kind === 'brain_unavailable'));
    }
    assert.equal(report.requested_cohort_verified, false);
    assert.deepEqual(fs.existsSync(db) ? fs.readFileSync(db) : null, before);
    assert.deepEqual(fs.readdirSync(root), files);
  });
}

test('inventory CLI exports JSON, signals unavailable evidence and rejects replay options', (t) => {
  const { root, db } = world(t);
  seed(db, [['one', now, 'NON_PASS', 'PASS']]);
  const args = [script, '--db', db, '--observed-at', String(now)];
  const result = execute(root, process.execPath, args);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), inventory({ db, observedAt: now }));
  const missing = path.join(root, 'absent', 'brain.db');
  const unavailable = execute(root, process.execPath,
    [script, '--db', missing, '--observed-at', String(now)]);
  assert.equal(unavailable.status, 2);
  assert.equal(JSON.parse(unavailable.stdout).included_count, null);
  assert.equal(fs.existsSync(path.dirname(missing)), false);
  for (const invalid of [['--db', db], [...args.slice(1), '--dispatch'],
    ['--db', db, '--observed-at', 'NaN'], [...args.slice(1), '--days', '0']]) {
    const failure = execute(root, process.execPath, [script, ...invalid]);
    assert.equal(failure.status, 1, failure.stderr);
    assert.equal(failure.stdout, '', 'invalid arguments cannot publish an inventory');
  }
});

function manifestFile(root, cases, schemaVersion = 1) {
  const file = path.join(root, 'requested.json');
  fs.writeFileSync(file, JSON.stringify({ schema_version: schemaVersion, cases }, null, 2));
  return file;
}

test('matching 71 identities bind the supplied manifest bytes without claiming historical acceptance', (t) => {
  const { root, db } = world(t);
  const cases = Array.from({ length: 71 }, (_, i) => ({
    run_id: `requested-${i}`, target: 'owner/repo#1',
  }));
  seed(db, cases.map(({ run_id }) => [run_id, now, 'NON_PASS', 'PASS']));
  const manifest = manifestFile(root, cases.reverse());
  const before = fs.readFileSync(db);
  const manifestBefore = fs.readFileSync(manifest);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now, manifest });
  const reconciliation = report.manifest_reconciliation;
  assert.equal(reconciliation.scope, 'supplied_manifest_only');
  assert.equal(reconciliation.manifest_path, manifest);
  assert.equal(reconciliation.manifest_sha256,
    createHash('sha256').update(manifestBefore).digest('hex'));
  assert.equal(reconciliation.requested_count, 71);
  assert.equal(reconciliation.count_delta, 0);
  assert.equal(reconciliation.exact_match, true);
  assert.deepEqual(reconciliation.cases.map(({ run_id, target }) => ({ run_id, target })), cases);
  assert.ok(reconciliation.cases.every((row) => row.status === 'included' && !row.reasons.length));
  assert.deepEqual(reconciliation.unexpected_cases, []);
  assert.equal(report.requested_cohort_verified, false);
  assert.deepEqual(report.evidence_gaps.map((gap) => gap.kind), ['requested_cohort_unverified']);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readFileSync(manifest), manifestBefore);
  assert.deepEqual(fs.readdirSync(root), files);
});

test('equal inventory counts expose substituted identities, exclusions and absent requested runs', (t) => {
  const { root, db } = world(t);
  seed(db, [
    ['included', now, 'NON_PASS', 'PASS'],
    ['unexpected-a', now, 'NON_PASS', 'PASS'],
    ['unexpected-b', now, 'NON_PASS', 'PASS'],
    ['excluded', cutoff - 1, 'PASS', 'PASS'],
  ]);
  const manifest = manifestFile(root, ['included', 'excluded', 'absent'].map((run_id) => ({
    run_id, target: 'owner/repo#1',
  })));
  const report = inventory({ db, observedAt: now, expectedCount: 3, manifest });
  assert.equal(report.count_delta, 0, 'count equality conceals substituted cases');
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.deepEqual(report.manifest_reconciliation.cases.map((row) => row.status),
    ['included', 'excluded', 'missing']);
  assert.deepEqual(report.manifest_reconciliation.unexpected_cases, [
    { run_id: 'unexpected-a', target: 'owner/repo#1' },
    { run_id: 'unexpected-b', target: 'owner/repo#1' },
  ]);
  assert.deepEqual(report.evidence_gaps.filter((gap) => gap.kind === 'requested_case_evidence_gap'), [
    { kind: 'requested_case_evidence_gap', run_id: 'excluded', target: 'owner/repo#1',
      reasons: ['outside_window', 'agreeing_verdicts'] },
    { kind: 'requested_case_evidence_gap', run_id: 'absent', target: 'owner/repo#1',
      reasons: ['requested_run_missing'] },
  ]);
  assert.ok(report.evidence_gaps.some((gap) => gap.kind === 'requested_manifest_discrepancy'));
});

test('a requested run on another target cannot satisfy case identity', (t) => {
  const { root, db } = world(t);
  seed(db, [['same-run', now, 'NON_PASS', 'PASS']]);
  const manifest = manifestFile(root, [{ run_id: 'same-run', target: 'owner/repo#2' }]);
  const report = inventory({ db, observedAt: now, expectedCount: 1, manifest });
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.deepEqual(report.manifest_reconciliation.cases, [{
    run_id: 'same-run', target: 'owner/repo#2', status: 'identity_mismatch',
    reasons: ['requested_target_mismatch'], observed_target: 'owner/repo#1',
  }]);
  assert.deepEqual(report.manifest_reconciliation.unexpected_cases,
    [{ run_id: 'same-run', target: 'owner/repo#1' }]);
});

test('inventory accounts for outcomes without run records as unavailable evidence', (t) => {
  const { root, db } = world(t);
  seed(db, [['included', now, 'NON_PASS', 'PASS'], ['no-outcome', now]]);
  const connection = new DatabaseSync(db);
  connection.exec(`
    INSERT INTO outcomes VALUES ('orphan-dispute', 'NON_PASS', 'PASS', 1, 'durable', ${now});
    INSERT INTO outcomes VALUES ('orphan-agreement', 'PASS', 'PASS', 1, 'durable', ${now});
  `);
  connection.close();
  const manifest = manifestFile(root, ['included', 'orphan-dispute'].map((run_id) => ({
    run_id, target: 'owner/repo#1',
  })));
  const before = fs.readFileSync(db);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now, expectedCount: 2, manifest });
  assert.equal(report.status, 'available');
  assert.equal(report.population_count, 4, 'every run and unmatched outcome is accounted for');
  assert.equal(report.included_count, 1);
  assert.equal(report.excluded_count, 3);
  assert.equal(report.orphaned_outcome_count, 2);
  assert.equal(report.population_count, report.included_count + report.excluded_count);
  assert.deepEqual(report.cases.map((row) => row.run_id), ['included']);
  assert.deepEqual(report.exclusions.filter((row) => row.run_id.startsWith('orphan-')), [
    { run_id: 'orphan-agreement', target: null, ts: null,
      verifier_verdict: 'PASS', adjudicated_verdict: 'PASS', merged: 1,
      durability: 'durable', durability_checked_ts: now,
      reasons: ['missing_run', 'missing_timestamp', 'agreeing_verdicts'] },
    { run_id: 'orphan-dispute', target: null, ts: null,
      verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS', merged: 1,
      durability: 'durable', durability_checked_ts: now,
      reasons: ['missing_run', 'missing_timestamp'] },
  ]);
  assert.deepEqual(report.evidence_gaps.filter((gap) => gap.kind === 'orphaned_outcome'), [
    { kind: 'orphaned_outcome', run_id: 'orphan-agreement',
      detail: 'No matching run record; target and replay-window membership are unknown.' },
    { kind: 'orphaned_outcome', run_id: 'orphan-dispute',
      detail: 'No matching run record; target and replay-window membership are unknown.' },
  ]);
  assert.deepEqual(report.manifest_reconciliation.cases, [
    { run_id: 'included', target: 'owner/repo#1', status: 'included', reasons: [] },
    { run_id: 'orphan-dispute', target: 'owner/repo#1', status: 'unavailable',
      reasons: ['missing_run', 'missing_timestamp'] },
  ]);
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.deepEqual(report.manifest_reconciliation.unexpected_cases, []);
  assert.equal(report.count_delta, -1);
  assert.equal(report.requested_cohort_verified, false);
  const result = execute(root, process.execPath, [script, '--db', db,
    '--observed-at', String(now), '--expected-count', '2', '--manifest', manifest]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), report);
  assert.deepEqual(fs.readFileSync(db), before);
  // execute() creates only its stdout/stderr captures, never a SQLite sidecar.
  assert.deepEqual(fs.readdirSync(root).filter((file) => !['stdout', 'stderr'].includes(file)), files);
});

test('an outcomes-only Brain is available with exclusions, rather than an observed empty store', (t) => {
  const { root, db } = world(t);
  const connection = new DatabaseSync(db);
  connection.exec(`INSERT INTO outcomes VALUES ('orphan', 'NON_PASS', 'PASS', 1, 'durable', ${now})`);
  connection.close();
  const before = fs.readFileSync(db);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now });
  assert.equal(report.status, 'available');
  assert.equal(report.population_count, 1);
  assert.equal(report.orphaned_outcome_count, 1);
  assert.equal(report.included_count, 0);
  assert.equal(report.excluded_count, 1);
  assert.equal(report.count_delta, -71);
  assert.equal(report.manifest_reconciliation, null);
  assert.deepEqual(report.cases, []);
  assert.deepEqual(report.exclusions[0].reasons, ['missing_run', 'missing_timestamp']);
  assert.deepEqual(report.evidence_gaps.map((gap) => gap.kind), [
    'requested_cohort_unverified', 'orphaned_outcome', 'inventory_count_discrepancy',
  ]);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readdirSync(root), files);
});

for (const unavailable of [false, true]) {
  test(`manifest retains every requested identity when Brain is ${unavailable ? 'unavailable' : 'empty'}`, (t) => {
    const { root, db } = world(t);
    if (unavailable) fs.unlinkSync(db);
    const manifest = manifestFile(root, ['one', 'two'].map((run_id) => ({
      run_id, target: 'owner/repo#1',
    })));
    const before = fs.readdirSync(root);
    const report = inventory({ db, observedAt: now, expectedCount: 2, manifest });
    assert.equal(report.manifest_reconciliation.exact_match, false);
    assert.deepEqual(report.manifest_reconciliation.cases.map((row) => row.run_id), ['one', 'two']);
    assert.ok(report.manifest_reconciliation.cases.every((row) =>
      row.status === (unavailable ? 'unavailable' : 'missing')));
    assert.equal(report.evidence_gaps.filter((gap) => gap.kind === 'requested_case_evidence_gap').length, 2);
    assert.deepEqual(report.manifest_reconciliation.unexpected_cases, unavailable ? null : []);
    assert.deepEqual(fs.readdirSync(root), before, 'a missing Brain must not be created');
  });
}

test('manifest count discrepancy prevents an exact match even when all supplied identities match', (t) => {
  const { root, db } = world(t);
  seed(db, [['one', now, 'NON_PASS', 'PASS']]);
  const manifest = manifestFile(root, [{ run_id: 'one', target: 'owner/repo#1' }]);
  const report = inventory({ db, observedAt: now, manifest });
  assert.equal(report.manifest_reconciliation.count_delta, -70);
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.ok(report.evidence_gaps.some((gap) => gap.kind === 'requested_manifest_discrepancy'));
});

test('manifest CLI preserves input bytes, rejects malformed identities and signals unavailable Brain', (t) => {
  const { root, db } = world(t);
  seed(db, [['one', now, 'NON_PASS', 'PASS']]);
  const manifest = manifestFile(root, [{ run_id: 'one', target: 'owner/repo#1' }]);
  const manifestBefore = fs.readFileSync(manifest);
  const dbBefore = fs.readFileSync(db);
  const args = [script, '--db', db, '--observed-at', String(now), '--manifest', manifest];
  const result = execute(root, process.execPath, args);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), inventory({ db, observedAt: now, manifest }));
  assert.deepEqual(fs.readFileSync(db), dbBefore);
  assert.deepEqual(fs.readFileSync(manifest), manifestBefore);
  fs.unlinkSync(db);
  const unavailable = execute(root, process.execPath, args);
  assert.equal(unavailable.status, 2);
  assert.equal(JSON.parse(unavailable.stdout).manifest_reconciliation.cases[0].status, 'unavailable');
  for (const value of [null, {}, { schema_version: 3, cases: [] },
    { schema_version: 1, cases: {} },
    ...[[null], [{}], [{ run_id: ' ', target: 'owner/repo#1' }],
      [{ run_id: 'one', target: 3 }], [{ run_id: 'one', target: ' ' }],
      [{ run_id: 'one', target: 'owner/repo#1' }, { run_id: 'one', target: 'owner/repo#2' }]]
      .map((cases) => ({ schema_version: 1, cases }))]) {
    fs.writeFileSync(manifest, JSON.stringify(value));
    const invalid = execute(root, process.execPath, args);
    assert.equal(invalid.status, 1, invalid.stderr);
    assert.equal(invalid.stdout, '', 'invalid manifest cannot publish a partial inventory');
  }
  fs.writeFileSync(manifest, '{broken json');
  assert.equal(execute(root, process.execPath, args).status, 1);
  fs.unlinkSync(manifest);
  const missing = execute(root, process.execPath, args);
  assert.equal(missing.status, 1);
  assert.equal(missing.stdout, '');
  assert.equal(fs.existsSync(db), false);
});

test('version 2 reconciles all 71 dispute identities without claiming historical provenance', (t) => {
  const { root, db } = world(t);
  const requested = Array.from({ length: 71 }, (_, i) => ({
    run_id: `dispute-${i}`, target: 'owner/repo#1',
    verifier_verdict: i < 57 ? 'NON_PASS' : 'FAIL_HOLLOW',
    adjudicated_verdict: i < 57 ? 'PASS' : 'FAIL',
  }));
  seed(db, requested.map((row) =>
    [row.run_id, now, row.verifier_verdict, row.adjudicated_verdict]));
  const manifest = manifestFile(root, requested, 2);
  const before = fs.readFileSync(db);
  const manifestBefore = fs.readFileSync(manifest);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now, manifest });
  const reconciled = report.manifest_reconciliation;
  assert.equal(reconciled.identity_scope, 'dispute');
  assert.equal(reconciled.exact_match, true);
  assert.deepEqual(reconciled.cases, requested.map((item) =>
    ({ ...item, status: 'included', reasons: [] })));
  assert.deepEqual(reconciled.unexpected_cases, []);
  assert.equal(reconciled.manifest_sha256, createHash('sha256').update(manifestBefore).digest('hex'));
  assert.equal(report.requested_cohort_verified, false);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readFileSync(manifest), manifestBefore);
  assert.deepEqual(fs.readdirSync(root), files);
});

for (const [verifier, adjudicated] of [['NON_PASS', 'FAIL'], ['FAIL_HOLLOW', 'PASS'], ['PASS', 'FAIL']]) {
  test(`version 2 exposes substituted verdicts ${verifier}/${adjudicated} despite matching run and count`, (t) => {
    const { root, db } = world(t);
    seed(db, [['same-run', now, verifier, adjudicated]]);
    const expected = { run_id: 'same-run', target: 'owner/repo#1',
      verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
    const manifest = manifestFile(root, [expected], 2);
    const before = fs.readFileSync(db);
    const report = inventory({ db, observedAt: now, expectedCount: 1, manifest });
    assert.equal(report.count_delta, 0);
    assert.equal(report.manifest_reconciliation.exact_match, false);
    assert.deepEqual(report.manifest_reconciliation.cases, [{ ...expected,
      status: 'identity_mismatch', reasons: ['requested_verdict_mismatch'],
      observed_target: 'owner/repo#1', observed_verifier_verdict: verifier,
      observed_adjudicated_verdict: adjudicated,
    }]);
    assert.deepEqual(report.manifest_reconciliation.unexpected_cases, [{
      run_id: 'same-run', target: 'owner/repo#1',
      verifier_verdict: verifier, adjudicated_verdict: adjudicated,
    }]);
    assert.deepEqual(report.evidence_gaps.filter((gap) => gap.kind === 'requested_case_evidence_gap'),
      [{ kind: 'requested_case_evidence_gap', ...expected, reasons: ['requested_verdict_mismatch'] }]);
    const result = execute(root, process.execPath, [script, '--db', db, '--observed-at', String(now),
      '--expected-count', '1', '--manifest', manifest]);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), report);
    assert.deepEqual(fs.readFileSync(db), before);
    // Legacy manifests remain readable, with their narrower identity scope explicit.
    manifestFile(root, [expected]);
    const legacy = inventory({ db, observedAt: now, expectedCount: 1, manifest });
    assert.equal(legacy.manifest_reconciliation.identity_scope, 'run_target');
    assert.equal(legacy.manifest_reconciliation.exact_match, true);
    assert.equal(legacy.requested_cohort_verified, false);
  });
}

test('version 2 retains different disputes on one run and rejects duplicate dispute identities', (t) => {
  const { root, db } = world(t);
  seed(db, [['same-run', now, 'NON_PASS', 'PASS']]);
  const original = { run_id: 'same-run', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  const changed = { ...original, verifier_verdict: 'FAIL_HOLLOW' };
  const manifest = manifestFile(root, [original, changed], 2);
  const report = inventory({ db, observedAt: now, expectedCount: 2, manifest });
  assert.deepEqual(report.manifest_reconciliation.cases.map((row) => row.status),
    ['included', 'identity_mismatch']);
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.deepEqual(report.manifest_reconciliation.unexpected_cases, []);
  for (const invalid of [original, { ...original, target: 'owner/repo#2' }]) {
    manifestFile(root, [original, invalid], 2);
    assert.throws(() => inventory({ db, observedAt: now, manifest }), /duplicate manifest identity/);
  }
});

test('version 2 preserves full identities for excluded, missing and unavailable cases', (t) => {
  const { root, db } = world(t);
  seed(db, [['old', cutoff - 1, 'NON_PASS', 'PASS']]);
  const requested = ['old', 'missing'].map((run_id) => ({ run_id, target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' }));
  const manifest = manifestFile(root, requested, 2);
  const report = inventory({ db, observedAt: now, expectedCount: 2, manifest });
  assert.deepEqual(report.manifest_reconciliation.cases, [
    { ...requested[0], status: 'excluded', reasons: ['outside_window'] },
    { ...requested[1], status: 'missing', reasons: ['requested_run_missing'] },
  ]);
  fs.unlinkSync(db);
  const result = execute(root, process.execPath, [script, '--db', db, '--observed-at', String(now),
    '--expected-count', '2', '--manifest', manifest]);
  assert.equal(result.status, 2, result.stderr);
  const unavailable = JSON.parse(result.stdout);
  assert.equal(unavailable.population_count, null);
  assert.equal(unavailable.manifest_reconciliation.unexpected_cases, null);
  assert.deepEqual(unavailable.manifest_reconciliation.cases, requested.map((item) =>
    ({ ...item, status: 'unavailable', reasons: ['brain_unavailable'] })));
  assert.equal(fs.existsSync(db), false);
});

test('version 2 rejects malformed verdicts before publishing any inventory', (t) => {
  const { root, db } = world(t);
  const original = { run_id: 'run', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  for (const field of ['verifier_verdict', 'adjudicated_verdict']) {
    for (const value of [undefined, null, '', ' ', 3, [], {}]) {
      const manifest = manifestFile(root, [{ ...original, [field]: value }], 2);
      const result = execute(root, process.execPath,
        [script, '--db', db, '--observed-at', String(now), '--manifest', manifest]);
      assert.equal(result.status, 1, result.stderr);
      assert.equal(result.stdout, '', 'malformed dispute identity cannot publish partial JSON');
      assert.match(result.stderr, /require nonblank verifier_verdict and adjudicated_verdict/);
    }
  }
});

test('version 2 distinguishes unavailable verdict evidence from contradictory verdicts', (t) => {
  const { root, db } = world(t);
  seed(db, [
    ['no-outcome', now],
    ['no-verifier', now, null, 'PASS'],
    ['no-adjudication', now, 'NON_PASS', null],
    ['neither-verdict', cutoff - 1, null, null],
    ['changed-verdict', now, 'NON_PASS', 'FAIL'],
  ]);
  const requested = ['no-outcome', 'no-verifier', 'no-adjudication', 'neither-verdict',
    'changed-verdict'].map((run_id) => ({ run_id, target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' }));
  const manifest = manifestFile(root, requested, 2);
  const before = fs.readFileSync(db);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now, expectedCount: 5, manifest });
  assert.equal(report.status, 'available', 'missing case evidence does not hide the observed population');
  assert.equal(report.population_count, 5);
  assert.equal(report.included_count, 1);
  assert.equal(report.excluded_count, 4);
  assert.equal(report.manifest_reconciliation.exact_match, false);
  const reasons = [
    ['missing_outcome'],
    ['missing_verifier_verdict'],
    ['missing_adjudicated_verdict'],
    ['outside_window', 'missing_verifier_verdict', 'missing_adjudicated_verdict'],
  ];
  assert.deepEqual(report.manifest_reconciliation.cases.slice(0, 4),
    requested.slice(0, 4).map((item, i) => ({ ...item, status: 'unavailable', reasons: reasons[i] })));
  assert.deepEqual(report.evidence_gaps.filter((gap) => gap.kind === 'requested_case_evidence_gap'),
    requested.map((item, i) => ({ kind: 'requested_case_evidence_gap', ...item,
      reasons: reasons[i] || ['requested_verdict_mismatch'] })));
  assert.equal(report.manifest_reconciliation.cases[4].status, 'identity_mismatch');
  assert.deepEqual(report.manifest_reconciliation.unexpected_cases, [{
    ...requested[4], adjudicated_verdict: 'FAIL',
  }]);
  assert.equal(report.requested_cohort_verified, false);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readdirSync(root), files);
  const result = execute(root, process.execPath, [script, '--db', db, '--observed-at', String(now),
    '--expected-count', '5', '--manifest', manifest]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), report);
  assert.deepEqual(fs.readFileSync(db), before);

  // Legacy run/target manifests can still identify an excluded run without
  // requiring the verdict pair that only version 2 promises to reconcile.
  manifestFile(root, requested);
  const legacy = inventory({ db, observedAt: now, expectedCount: 5, manifest });
  assert.deepEqual(legacy.manifest_reconciliation.cases.map((row) => row.status),
    ['excluded', 'excluded', 'excluded', 'excluded', 'included']);
});

test('version 2 retains an observed target mismatch when verdict evidence is unavailable', (t) => {
  const { root, db } = world(t);
  seed(db, [['no-outcome', now]]);
  const requested = { run_id: 'no-outcome', target: 'owner/repo#2',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  const manifest = manifestFile(root, [requested], 2);
  const report = inventory({ db, observedAt: now, expectedCount: 1, manifest });
  assert.deepEqual(report.manifest_reconciliation.cases, [{ ...requested,
    status: 'identity_mismatch', reasons: ['requested_target_mismatch'],
    observed_target: 'owner/repo#1', observed_verifier_verdict: null,
    observed_adjudicated_verdict: null,
  }]);
  assert.equal(report.manifest_reconciliation.exact_match, false);
});

test('missing stored targets remain eligible disputes with explicit unavailable identity evidence', (t) => {
  const { root, db } = world(t);
  seed(db, [
    ['null-target', now, 'NON_PASS', 'PASS'],
    ['empty-target', now, 'NON_PASS', 'PASS'],
    ['blank-target', now, 'NON_PASS', 'PASS'],
    ['excluded-target', cutoff - 1],
    ['other-target', now, 'NON_PASS', 'PASS'],
    ['complete', now, 'NON_PASS', 'PASS'],
  ]);
  const connection = new DatabaseSync(db);
  for (const [id, target] of [
    ['null-target', null], ['empty-target', ''], ['blank-target', ' \t '],
    ['excluded-target', null], ['other-target', 'owner/repo#2'],
  ]) connection.prepare('UPDATE runs SET target=? WHERE run_id=?').run(target, id);
  connection.close();
  const requested = ['null-target', 'empty-target', 'blank-target', 'excluded-target',
    'other-target', 'complete'].map((run_id) => ({ run_id, target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' }));
  const before = fs.readFileSync(db);
  const observed = inventory({ db, observedAt: now, expectedCount: 6 });
  assert.equal(observed.population_count, 6);
  assert.equal(observed.included_count, 5, 'preserve production replay selection');
  assert.equal(observed.excluded_count, 1);
  assert.equal(observed.evidence_gaps.filter((gap) => gap.kind === 'missing_target').length, 4,
    'missing targets must be visible even without a supplied manifest');
  const reference = execute(root, process.env.PYTHON || 'python3', ['-c', `
import json
from pathlib import Path
import adjudicator_retro as retro
print(json.dumps(retro.disputes(db=Path(${JSON.stringify(db)}), now=${now})))
`]);
  assert.equal(reference.status, 0, reference.stderr);
  assert.deepEqual(observed.cases, JSON.parse(reference.stdout));
  for (const version of [1, 2]) {
    const manifest = manifestFile(root, requested, version);
    const files = fs.readdirSync(root);
    const report = inventory({ db, observedAt: now, expectedCount: 6, manifest });
    const reconciled = report.manifest_reconciliation;
    const identities = requested.map(({ run_id, target, ...verdicts }) => ({
      run_id, target, ...(version === 2 ? verdicts : {}),
    }));
    const reasons = [
      ['missing_target'], ['missing_target'], ['missing_target'],
      ['outside_window', 'missing_outcome', 'missing_target'],
    ];
    assert.deepEqual(reconciled.cases.slice(0, 4), identities.slice(0, 4).map((item, i) =>
      ({ ...item, status: 'unavailable', reasons: reasons[i] })));
    assert.equal(reconciled.cases[4].status, 'identity_mismatch',
      'a nonblank contradictory target still represents an identity mismatch');
    assert.equal(reconciled.cases[5].status, 'included', 'continue to the next intact case');
    assert.equal(reconciled.exact_match, false);
    assert.equal(report.requested_cohort_verified, false);
    assert.deepEqual(report.evidence_gaps.filter((gap) => gap.kind === 'requested_case_evidence_gap')
      .slice(0, 4), identities.slice(0, 4).map((item, i) =>
      ({ kind: 'requested_case_evidence_gap', ...item, reasons: reasons[i] })));
    const result = execute(root, process.execPath, [script, '--db', db,
      '--observed-at', String(now), '--expected-count', '6', '--manifest', manifest]);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), report);
    assert.deepEqual(fs.readFileSync(db), before);
    assert.deepEqual(fs.readdirSync(root), files, 'no report or SQLite sidecars created');
  }
});
