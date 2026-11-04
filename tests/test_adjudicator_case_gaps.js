'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { DatabaseSync } = require('node:sqlite');
const { test } = require('node:test');
const { caseGaps } = require('../scripts/export_adjudicator_case_gaps');

const script = path.resolve(__dirname, '../scripts/export_adjudicator_case_gaps.js');
const now = 2000000000;
const identity = (run_id, target = 'fixture/repo#1') => ({
  run_id, target, verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS',
});

function world(t, cases, schema = 2) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-case-gaps-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const db = path.join(root, 'brain.db');
  const manifest = path.join(root, 'manifest.json');
  fs.writeFileSync(manifest, JSON.stringify({ schema_version: schema, cases }));
  const connection = new DatabaseSync(db);
  connection.exec(`
    CREATE TABLE runs (run_id TEXT PRIMARY KEY, target TEXT, ts INTEGER);
    CREATE TABLE outcomes (run_id TEXT PRIMARY KEY, verifier_verdict TEXT,
      adjudicated_verdict TEXT, merged INTEGER, durability TEXT,
      durability_checked_ts INTEGER);
  `);
  connection.close();
  return { root, db, manifest, observedAt: now, expectedCount: cases.length };
}

function invoke(options, extra = []) {
  const stdoutPath = path.join(options.root, 'gap-ledger.json');
  const stderrPath = path.join(options.root, 'stderr');
  const stdout = fs.openSync(stdoutPath, 'w');
  const stderr = fs.openSync(stderrPath, 'w');
  let result;
  try {
    result = spawnSync(process.execPath, [script, '--db', options.db,
      '--observed-at', String(options.observedAt), '--expected-count', String(options.expectedCount),
      '--manifest', options.manifest, ...extra], {
      cwd: options.root, timeout: 10000, stdio: ['ignore', stdout, stderr],
    });
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
  }
  assert.ifError(result.error);
  return { status: result.status, stdout: fs.readFileSync(stdoutPath, 'utf8'),
    stderr: fs.readFileSync(stderrPath, 'utf8') };
}

test('gap export preserves every dispute and explicit UNKNOWN execution evidence without writes', (t) => {
  const requested = ['included', 'missing', 'old', 'incomplete', 'changed-target',
    'changed-verdict'].map((id) => identity(id));
  // A second requested dispute on the same run is a distinct obligation.
  requested.push({ ...identity('included'), adjudicated_verdict: 'FAIL' });
  const options = world(t, requested);
  const connection = new DatabaseSync(options.db);
  for (const id of ['included', 'old', 'incomplete', 'changed-target', 'changed-verdict']) {
    connection.prepare('INSERT INTO runs VALUES (?,?,?)').run(id,
      id === 'changed-target' ? 'fixture/repo#2' : 'fixture/repo#1',
      id === 'old' ? now - 100 * 86400 : now);
    if (id !== 'incomplete') connection.prepare('INSERT INTO outcomes VALUES (?,?,?,?,?,?)')
      .run(id, 'NON_PASS', id === 'changed-verdict' ? 'FAIL' : 'PASS', 1, 'durable', now);
  }
  connection.close();
  const dbBefore = fs.readFileSync(options.db);
  const manifestBefore = fs.readFileSync(options.manifest);
  const filesBefore = fs.readdirSync(options.root);
  const ledger = caseGaps(options);
  assert.deepEqual(ledger.cases.map((row) => ({ run_id: row.run_id, target: row.target,
    verifier_verdict: row.verifier_verdict, adjudicated_verdict: row.adjudicated_verdict })), requested);
  assert.deepEqual(ledger.cases.map((row) => row.inventory_status), [
    'included', 'missing', 'excluded', 'unavailable', 'identity_mismatch',
    'identity_mismatch', 'identity_mismatch',
  ]);
  assert.deepEqual(ledger.cases.map((row) => row.inventory_reasons), [
    [], ['requested_run_missing'], ['outside_window'], ['missing_outcome'],
    ['requested_target_mismatch'], ['requested_verdict_mismatch'], ['requested_verdict_mismatch'],
  ]);
  for (const row of ledger.cases) {
    assert.equal(row.status, 'evidence_gap');
    for (const field of ['source_revision', 'shadow_result', 'role_run_provenance', 'cost_usd']) {
      assert.equal(row[field], 'UNKNOWN', `${row.run_id}: ${field}`);
    }
    assert.deepEqual(row.evidence_gaps, [...row.inventory_reasons, 'shadow_evidence_not_collected']);
  }
  assert.equal(ledger.cases[4].observed_target, 'fixture/repo#2');
  assert.equal(ledger.cases[5].observed_adjudicated_verdict, 'FAIL');
  assert.equal(ledger.supplied_case_count, 7);
  assert.equal(ledger.evidence_gap_count, 7);
  assert.equal(ledger.unsupplied_requested_slots, 0);
  assert.equal(ledger.requested_cohort_verified, false);
  assert.equal(ledger.authenticated_requested_case_count, 'UNKNOWN');
  assert.equal(ledger.operational_acceptance_complete, false);
  assert.equal(ledger.inventory_snapshot.manifest_reconciliation.manifest_sha256,
    createHash('sha256').update(manifestBefore).digest('hex'));
  assert.equal(ledger.collector_sha256, createHash('sha256').update(fs.readFileSync(script)).digest('hex'));
  assert.equal(ledger.inventory_collector_sha256, createHash('sha256')
    .update(fs.readFileSync(path.resolve(__dirname, '../scripts/export_adjudicator_inventory.js'))).digest('hex'));
  assert.deepEqual(fs.readdirSync(options.root), filesBefore);
  const cli = invoke(options);
  assert.equal(cli.status, 0, cli.stderr);
  assert.deepEqual(JSON.parse(cli.stdout), ledger, 'file-backed stdout preserves every case');
  assert.deepEqual(fs.readFileSync(options.db), dbBefore);
  assert.deepEqual(fs.readFileSync(options.manifest), manifestBefore);
  assert.deepEqual(fs.readdirSync(options.root).sort(), [...filesBefore, 'gap-ledger.json', 'stderr'].sort());
});

test('owner-excluded evidence is never evaluated and remains an identity-only gap', (t) => {
  const excludedTarget = '\tStRaNsKe/CoLlAb-AdMiN\u00a0#1 ';
  const requested = [identity('excluded', excludedTarget), identity('allowed')];
  const options = world(t, requested);
  const connection = new DatabaseSync(options.db);
  connection.prepare('INSERT INTO runs VALUES (?,?,?)').run('excluded', excludedTarget, now);
  connection.exec(`
    INSERT INTO runs VALUES ('allowed','fixture/repo#1',${now});
    ALTER TABLE runs RENAME TO run_rows;
    CREATE VIEW runs AS SELECT run_id,target,
      CASE WHEN run_id='excluded' THEN json_extract('unreadable','$') ELSE ts END AS ts
      FROM run_rows;
    INSERT INTO outcomes VALUES ('excluded','NON_PASS','PASS',1,'durable',${now}),
                                ('allowed','NON_PASS','PASS',1,'durable',${now});
    ALTER TABLE outcomes RENAME TO outcome_rows;
    CREATE VIEW outcomes AS SELECT run_id,
      CASE WHEN run_id='excluded' THEN json_extract('unreadable','$')
        ELSE verifier_verdict END AS verifier_verdict,
      adjudicated_verdict,merged,durability,durability_checked_ts FROM outcome_rows;
  `);
  assert.throws(() => connection.prepare("SELECT ts FROM runs WHERE run_id='excluded'").get(), /malformed JSON/);
  assert.throws(() => connection.prepare("SELECT verifier_verdict FROM outcomes WHERE run_id='excluded'").get(), /malformed JSON/);
  connection.close();
  const before = fs.readFileSync(options.db);
  const result = invoke(options);
  assert.equal(result.status, 0, result.stderr);
  const ledger = JSON.parse(result.stdout);
  assert.equal(ledger.inventory_snapshot.status, 'available');
  assert.deepEqual(ledger.cases[0], {
    ...requested[0], status: 'evidence_gap', inventory_status: 'excluded',
    inventory_reasons: ['owner_excluded_repository'], source_revision: 'UNKNOWN',
    shadow_result: 'UNKNOWN', role_run_provenance: 'UNKNOWN', cost_usd: 'UNKNOWN',
    evidence_gaps: ['owner_excluded_repository'],
  });
  assert.equal(ledger.cases[1].inventory_status, 'included');
  assert.deepEqual(fs.readFileSync(options.db), before);
});

for (const version of [1, 2]) {
  test(`schema ${version} retains requested identities and unknown counts with an unavailable Brain`, (t) => {
    const requested = [identity('missing'), identity('excluded', '\u2003stranske/Collab-Admin\t#2')];
    const options = world(t, requested, version);
    fs.unlinkSync(options.db);
    options.expectedCount = 71;
    const before = fs.readFileSync(options.manifest);
    const result = invoke(options);
    assert.equal(result.status, 2, result.stderr);
    const ledger = JSON.parse(result.stdout);
    assert.equal(ledger.supplied_case_count, 2);
    assert.equal(ledger.unsupplied_requested_slots, 69);
    assert.equal(ledger.evidence_gap_count, 2);
    assert.deepEqual(ledger.cases.map((row) => row.run_id), ['missing', 'excluded']);
    assert.deepEqual(ledger.cases[0].evidence_gaps, ['brain_unavailable', 'shadow_evidence_not_collected']);
    assert.deepEqual(ledger.cases[1].evidence_gaps, ['owner_excluded_repository']);
    for (const key of ['population_count', 'included_count', 'excluded_count', 'count_delta']) {
      assert.equal(ledger.inventory_snapshot[key], null);
    }
    assert.equal(ledger.requested_cohort_verified, false);
    assert.equal(ledger.operational_acceptance_complete, false);
    assert.equal(fs.existsSync(options.db), false);
    assert.deepEqual(fs.readFileSync(options.manifest), before);
  });
}

test('an empty supplied manifest cannot claim recovery or hide the missing requested slots', (t) => {
  const options = world(t, []);
  const ledger = caseGaps({ ...options, expectedCount: 71 });
  assert.equal(ledger.unsupplied_requested_slots, 71);
  assert.equal(ledger.authenticated_requested_case_count, 'UNKNOWN');
  assert.equal(ledger.requested_cohort_verified, false);
  assert.equal(ledger.operational_acceptance_complete, false);
  assert.deepEqual(ledger.cases, []);
  assert.match(ledger.limitations.join('\n'), /No supplied identities/);
  assert.ok(ledger.inventory_snapshot.evidence_gaps.some((row) => row.kind === 'requested_manifest_discrepancy'));
});

test('invalid arguments and manifests produce no partial ledger or Brain writes', (t) => {
  const options = world(t, [identity('one')]);
  const before = fs.readFileSync(options.db);
  assert.throws(() => caseGaps({ ...options, manifest: undefined }), /manifest is required/);
  for (const content of ['{broken', 'null', JSON.stringify({ schema_version: 2, cases: [identity('one'), identity('one')] }),
    JSON.stringify({ schema_version: 2, cases: [{ run_id: 'one', target: 'fixture/repo#1' }] })]) {
    fs.writeFileSync(options.manifest, content);
    const result = invoke(options);
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.ok(result.stderr);
    assert.equal(fs.readFileSync(options.manifest, 'utf8'), content);
    assert.deepEqual(fs.readFileSync(options.db), before);
  }
  fs.writeFileSync(options.manifest, JSON.stringify({ schema_version: 2, cases: [identity('one')] }));
  for (const args of [['--observed-at', 'not-a-time'], ['--expected-count', '-1'], ['--days', '0'], ['--unknown']]) {
    const result = invoke(options, args);
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.deepEqual(fs.readFileSync(options.db), before);
  }
});

function savedReport(options, rows) {
  const file = path.join(options.root, 'saved-report.json');
  fs.writeFileSync(file, JSON.stringify({ rows, summary: { cost_usd: 999 } }));
  return file;
}

test('saved report joins the complete dispute identity and binds unverified claims to retained bytes', (t) => {
  const requested = [identity('one'), identity('other-target', 'fixture/repo#2'),
    { ...identity('one'), adjudicated_verdict: 'FAIL' }, identity('missing')];
  const options = world(t, requested);
  const rows = [
    { ...requested[1], cost_usd: 4 },
    { ...requested[0], role_run_id: 'role:one', backend_run_id: 'backend:one',
      decision: 'reject_blocker', cost_usd: 0,
      packet: { target: requested[0].target, ground_truth_evidence: { head_sha: 'a'.repeat(40) } } },
    { ...requested[2], role_run_id: 'role:fail', cost_usd: null, error: 'Missing bound evidence' },
    identity('one', 'fixture/repo#99'),
  ];
  const report = savedReport(options, rows);
  const before = [options.db, options.manifest, report].map((file) => fs.readFileSync(file));
  const filesBefore = fs.readdirSync(options.root);
  const ledger = caseGaps({ ...options, report });
  assert.deepEqual(ledger.cases.map((row) => row.saved_report_evidence?.row_index), [1, 0, 2, undefined]);
  const evidence = ledger.cases[0].saved_report_evidence;
  assert.equal(evidence.status, 'unverified_saved_report_row');
  assert.equal(evidence.report.path, report);
  assert.equal(evidence.report.sha256, createHash('sha256').update(before[2]).digest('hex'));
  assert.equal(evidence.row_sha256, createHash('sha256').update(JSON.stringify(rows[1])).digest('hex'));
  assert.equal(evidence.reported_source_revision, 'a'.repeat(40));
  assert.equal(evidence.reported_role_run_id, 'role:one');
  assert.equal(evidence.reported_backend_run_id, 'backend:one');
  assert.equal(evidence.reported_decision, 'reject_blocker');
  assert.equal(evidence.reported_cost_usd, 0, 'reported zero is retained without admitting it as measured');
  for (const row of ledger.cases) {
    for (const field of ['source_revision', 'shadow_result', 'role_run_provenance', 'cost_usd']) {
      assert.equal(row[field], 'UNKNOWN');
    }
  }
  assert.ok(ledger.cases[2].evidence_gaps.includes('saved_case_error'));
  assert.ok(ledger.cases[2].evidence_gaps.includes('saved_backend_run_id_missing'));
  assert.equal(ledger.cases[2].saved_report_evidence.reported_cost_usd, 'UNKNOWN');
  assert.ok(ledger.cases[3].evidence_gaps.includes('saved_case_missing'));
  assert.equal(ledger.saved_report_snapshot.row_count, 4);
  assert.equal(ledger.saved_report_snapshot.matched_case_count, 3);
  assert.equal(ledger.saved_report_snapshot.unmatched_row_count, 1);
  assert.equal(ledger.saved_report_snapshot.persisted_records_verified, false);
  assert.equal(ledger.requested_cohort_verified, false);
  assert.equal(ledger.operational_acceptance_complete, false);
  assert.match(ledger.limitations.join('\n'), /persisted role records and measured costs remain unverified/);
  assert.deepEqual(fs.readdirSync(options.root), filesBefore);
  const cli = invoke(options, ['--report', report]);
  assert.equal(cli.status, 0, cli.stderr);
  assert.deepEqual(JSON.parse(cli.stdout), ledger);
  assert.deepEqual([options.db, options.manifest, report].map((file) => fs.readFileSync(file)), before);
});

test('ambiguous saved rows and owner-excluded cases cannot contribute execution claims', (t) => {
  const requested = [identity('duplicate'), identity('excluded', 'stranske/Collab-Admin#1')];
  const options = world(t, requested);
  const report = savedReport(options, [requested[0], { ...requested[0], role_run_id: 'other' },
    { ...requested[1], role_run_id: 'excluded-role', cost_usd: 100 }]);
  const ledger = caseGaps({ ...options, report });
  assert.ok(ledger.cases[0].evidence_gaps.includes('saved_case_ambiguous'));
  assert.equal(ledger.cases[0].saved_report_evidence, undefined);
  assert.equal(ledger.cases[1].saved_report_evidence, undefined);
  assert.deepEqual(ledger.cases[1].evidence_gaps, ['owner_excluded_repository']);
  assert.equal(ledger.saved_report_snapshot.matched_case_count, 0);
  assert.equal(ledger.saved_report_snapshot.unmatched_row_count, 3);
  assert.equal(JSON.stringify(ledger).includes('excluded-role'), false);
});

test('mismatched packets, invalid revisions and invalid costs remain specific gaps', (t) => {
  const options = world(t, [identity('one')]);
  for (const packet of [undefined,
    { target: 'fixture/repo#2', ground_truth_evidence: { head_sha: 'a'.repeat(40) } },
    { target: 'fixture/repo#1', ground_truth_evidence: { head_sha: 'abc123' } }]) {
    for (const cost_usd of [null, -1, '2.5']) {
      const report = savedReport(options, [{ ...identity('one'), packet, cost_usd }]);
      const row = caseGaps({ ...options, report }).cases[0];
      assert.equal(row.saved_report_evidence.reported_source_revision, 'UNKNOWN');
      assert.equal(row.saved_report_evidence.reported_cost_usd, 'UNKNOWN');
      assert.ok(row.evidence_gaps.includes('source_revision_unavailable'));
      assert.ok(row.evidence_gaps.includes('saved_role_run_id_missing'));
      assert.ok(row.evidence_gaps.includes('measured_cost_not_verified'));
      assert.equal(row.evidence_gaps.includes('saved_packet_target_missing'), !packet);
      assert.equal(row.evidence_gaps.includes('saved_packet_target_mismatch'), packet?.target === 'fixture/repo#2');
    }
  }
});

test('invalid saved reports and run-only manifests fail without partial output or writes', (t) => {
  const options = world(t, [identity('one')]);
  const report = savedReport(options, []);
  const before = fs.readFileSync(options.db);
  for (const text of ['{broken', 'null', '{}', '{"rows":[null]}', '{"rows":[{"run_id":"one"}]}']) {
    fs.writeFileSync(report, text);
    const result = invoke(options, ['--report', report]);
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.deepEqual(fs.readFileSync(options.db), before);
    assert.equal(fs.readFileSync(report, 'utf8'), text);
  }
  fs.writeFileSync(report, JSON.stringify({ rows: [identity('one')] }));
  fs.writeFileSync(options.manifest, JSON.stringify({ schema_version: 1, cases: [identity('one')] }));
  const result = invoke(options, ['--report', report]);
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /requires a schema_version 2 dispute manifest/);
  assert.deepEqual(fs.readFileSync(options.db), before);
});
