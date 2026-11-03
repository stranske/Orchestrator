'use strict';

// Read-only snapshot of the Brain selection used by adjudicator_retro.disputes,
// plus explicit exclusions for runs or outcomes missing their matching row.
// Requires Node >=22.13 (built-in SQLite). JSON goes to stdout; callers choose
// where to preserve it. This tool never opens the retrospective report or roles.
const path = require('node:path');
const fs = require('node:fs');
const { createHash } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { parseArgs } = require('node:util');

function integer(value, name, minimum) {
  if (!Number.isSafeInteger(value) || value < minimum) {
    throw new Error(`${name} must be a safe integer >= ${minimum}`);
  }
  return value;
}

function hasTarget(row) {
  return typeof row.target === 'string' && Boolean(row.target.trim());
}

function readManifest(file) {
  if (typeof file !== 'string' || !file.trim()) throw new Error('manifest path is required');
  const bytes = fs.readFileSync(file);
  const manifest = JSON.parse(bytes);
  if (![1, 2].includes(manifest?.schema_version) || !Array.isArray(manifest.cases)) {
    throw new Error('manifest requires schema_version 1 or 2 and a cases array');
  }
  const seen = new Set();
  for (const item of manifest.cases) {
    if (!item || typeof item.run_id !== 'string' || !item.run_id.trim() ||
        typeof item.target !== 'string' || !item.target.trim()) {
      throw new Error('manifest cases require nonblank run_id and target strings');
    }
    if (manifest.schema_version === 2 &&
        ['verifier_verdict', 'adjudicated_verdict'].some((key) =>
          typeof item[key] !== 'string' || !item[key].trim())) {
      throw new Error('schema_version 2 cases require nonblank verifier_verdict and adjudicated_verdict strings');
    }
    // The retrospective keys a dispute by the run and both verdicts. A run
    // can appear more than once only when the requested disputes differ.
    const identity = manifest.schema_version === 1 ? item.run_id :
      JSON.stringify([item.run_id, item.verifier_verdict, item.adjudicated_verdict]);
    if (seen.has(identity)) throw new Error(`duplicate manifest identity: ${item.run_id}`);
    seen.add(identity);
  }
  return { path: path.resolve(file), sha256: createHash('sha256').update(bytes).digest('hex'),
    schema_version: manifest.schema_version,
    cases: manifest.cases.map(({ run_id, target, verifier_verdict, adjudicated_verdict }) => ({
      run_id, target,
      ...(manifest.schema_version === 2 ? { verifier_verdict, adjudicated_verdict } : {}),
    })) };
}

function reconcile(report, manifest) {
  if (!manifest) return report;
  const observed = new Map([...(report.cases || []), ...(report.exclusions || [])]
    .map((row) => [row.run_id, row]));
  const verdictsMatch = (requested, row) => manifest.schema_version === 1 ||
    (requested.verifier_verdict === row.verifier_verdict &&
      requested.adjudicated_verdict === row.adjudicated_verdict);
  const requested = new Map();
  for (const item of manifest.cases) {
    const entries = requested.get(item.run_id) || [];
    entries.push(item);
    requested.set(item.run_id, entries);
  }
  const cases = manifest.cases.map((item) => {
    const row = observed.get(item.run_id);
    let status;
    let reasons;
    if (report.status === 'unavailable') {
      status = 'unavailable'; reasons = ['brain_unavailable'];
    } else if (!row) {
      status = 'missing'; reasons = ['requested_run_missing'];
    } else if (row.reasons?.includes('missing_run')) {
      // The outcome exists, but without its run we cannot verify the target
      // or timestamp. Do not misreport this as a different observed target.
      status = 'unavailable'; reasons = row.reasons;
    } else if (!hasTarget(row)) {
      // A stored run without a target cannot establish a contradictory PR
      // identity. Preserve other gaps without changing replay eligibility.
      status = 'unavailable'; reasons = [...(row.reasons || []), 'missing_target'];
    } else if (row.target !== item.target) {
      status = 'identity_mismatch'; reasons = ['requested_target_mismatch'];
    } else if (manifest.schema_version === 2 && row.reasons?.some((reason) =>
      ['missing_outcome', 'missing_verifier_verdict', 'missing_adjudicated_verdict'].includes(reason))) {
      // A missing verdict cannot contradict the requested pair. Preserve all
      // observed exclusion reasons so evidence recovery can target the gap.
      status = 'unavailable'; reasons = row.reasons;
    } else if (!verdictsMatch(item, row)) {
      status = 'identity_mismatch'; reasons = ['requested_verdict_mismatch'];
    } else if (row.reasons) {
      status = 'excluded'; reasons = row.reasons;
    } else {
      status = 'included'; reasons = [];
    }
    if (reasons.length) report.evidence_gaps.push({
      kind: 'requested_case_evidence_gap', ...item, reasons,
    });
    return { ...item, status, reasons,
      ...(status === 'identity_mismatch' ? { observed_target: row.target,
        ...(manifest.schema_version === 2 ? {
          observed_verifier_verdict: row.verifier_verdict,
          observed_adjudicated_verdict: row.adjudicated_verdict,
        } : {}) } : {}) };
  });
  const unexpected = report.cases === null ? null : report.cases
    .filter((row) => !(requested.get(row.run_id) || []).some((item) =>
      item.target === row.target && verdictsMatch(item, row)))
    .map(({ run_id, target, verifier_verdict, adjudicated_verdict }) => ({ run_id, target,
      ...(manifest.schema_version === 2 ? { verifier_verdict, adjudicated_verdict } : {}),
    }));
  const countDelta = manifest.cases.length - report.expected_count;
  const exactMatch = report.status === 'available' && countDelta === 0 &&
    cases.every((row) => row.status === 'included') && unexpected.length === 0;
  report.manifest_reconciliation = {
    scope: 'supplied_manifest_only',
    identity_scope: manifest.schema_version === 2 ? 'dispute' : 'run_target',
    manifest_path: manifest.path, manifest_sha256: manifest.sha256,
    requested_count: cases.length, count_delta: countDelta,
    exact_match: exactMatch, cases, unexpected_cases: unexpected,
  };
  if (countDelta !== 0 || (report.status === 'available' && !exactMatch)) {
    report.evidence_gaps.push({
      kind: 'requested_manifest_discrepancy',
      detail: 'The supplied manifest differs from the requested count or observed eligible case identities; inspect per-case statuses and unexpected_cases.',
    });
  }
  return report;
}

function inventory({ db, observedAt, days = 90, expectedCount = 71, manifest }) {
  if (typeof db !== 'string' || !db.trim()) throw new Error('db is required');
  integer(observedAt, 'observedAt', 0);
  integer(days, 'days', 1);
  integer(expectedCount, 'expectedCount', 0);
  const cutoff = observedAt - days * 86400;
  integer(cutoff, 'cutoff', 0);
  const requestedManifest = manifest === undefined ? null : readManifest(manifest);
  const report = {
    schema_version: 1,
    scope: 'observed_brain_window',
    database: path.resolve(db),
    observed_at: observedAt,
    days,
    cutoff_inclusive: cutoff,
    // Match the replay reader exactly: its selection has no upper time bound.
    upper_time_bound: null,
    expected_count: expectedCount,
    status: 'unavailable',
    population_count: null,
    included_count: null,
    excluded_count: null,
    orphaned_outcome_count: null,
    count_delta: null,
    requested_cohort_verified: false,
    manifest_reconciliation: null,
    cases: null,
    exclusions: null,
    evidence_gaps: [{
      kind: 'requested_cohort_unverified',
      // A caller-supplied file cannot establish historical provenance by itself.
      detail: requestedManifest
        ? 'Supplied-manifest reconciliation does not establish that the manifest identifies the original requested cohort.'
        : 'This window snapshot is not bound to the original requested case manifest; count equality alone cannot verify the cohort.',
    }],
  };
  let connection;
  let rows;
  try {
    connection = new DatabaseSync(report.database, { readOnly: true });
    // One statement supplies a consistent snapshot for counts and exclusions.
    // SQL computes eligibility with the same null and comparison semantics as
    // the Python reader. Inventory both sides: the Brain schema permits an
    // outcome with no run, whose target and window membership remain unknown.
    rows = connection.prepare(`
      WITH population AS (
        SELECT r.run_id,r.target,r.ts,o.verifier_verdict,o.adjudicated_verdict,
               o.merged,o.durability,o.durability_checked_ts,
               1 AS has_run,o.run_id IS NOT NULL AS has_outcome
        FROM runs r LEFT JOIN outcomes o USING(run_id)
        UNION ALL
        SELECT o.run_id,NULL,NULL,o.verifier_verdict,o.adjudicated_verdict,
               o.merged,o.durability,o.durability_checked_ts,0,1
        FROM outcomes o WHERE NOT EXISTS (
          SELECT 1 FROM runs r WHERE r.run_id=o.run_id
        )
      )
      SELECT *,ts >= ? AS in_window,
             verifier_verdict = adjudicated_verdict AS verdicts_agree
      FROM population ORDER BY ts DESC,run_id
    `).all(cutoff);
  } catch (error) {
    report.evidence_gaps.push({ kind: 'brain_unavailable', detail: error.message });
    return reconcile(report, requestedManifest);
  } finally {
    if (connection) connection.close();
  }
  const cases = [];
  const exclusions = [];
  let orphanedOutcomes = 0;
  for (const item of rows) {
    const { has_run: hasRun, has_outcome: hasOutcome, in_window: inWindow,
      verdicts_agree: verdictsAgree, ...row } = item;
    const reasons = [];
    if (!hasRun) {
      reasons.push('missing_run');
      orphanedOutcomes += 1;
      report.evidence_gaps.push({
        kind: 'orphaned_outcome', run_id: row.run_id,
        detail: 'No matching run record; target and replay-window membership are unknown.',
      });
    } else if (!hasTarget(row)) {
      report.evidence_gaps.push({
        kind: 'missing_target', run_id: row.run_id, target: row.target,
        detail: 'The run has no nonblank target; its requested PR identity cannot be verified.',
      });
    }
    if (!inWindow) reasons.push(row.ts === null ? 'missing_timestamp' : 'outside_window');
    if (!hasOutcome) reasons.push('missing_outcome');
    else {
      if (row.verifier_verdict === null) reasons.push('missing_verifier_verdict');
      if (row.adjudicated_verdict === null) reasons.push('missing_adjudicated_verdict');
      if (verdictsAgree) reasons.push('agreeing_verdicts');
    }
    if (reasons.length) exclusions.push({ ...row, reasons });
    else cases.push(row);
  }
  Object.assign(report, {
    status: 'available', population_count: rows.length,
    included_count: cases.length, excluded_count: exclusions.length,
    orphaned_outcome_count: orphanedOutcomes,
    count_delta: cases.length - expectedCount, cases, exclusions,
  });
  if (report.count_delta !== 0) {
    report.evidence_gaps.push({
      kind: 'inventory_count_discrepancy',
      detail: `Observed ${cases.length} eligible disputes; requested ${expectedCount}. The identities explaining the difference remain unverified.`,
    });
  }
  return reconcile(report, requestedManifest);
}

function main() {
  try {
    const { values } = parseArgs({ options: {
      db: { type: 'string' },
      'observed-at': { type: 'string' },
      days: { type: 'string', default: '90' },
      'expected-count': { type: 'string', default: '71' },
      manifest: { type: 'string' },
      help: { type: 'boolean', short: 'h' },
    } });
    if (values.help) {
      process.stdout.write('Usage: node scripts/export_adjudicator_inventory.js --db BRAIN --observed-at UNIX_SECONDS [--days 90] [--expected-count 71] [--manifest JSON_FILE]\nManifest: {"schema_version":2,"cases":[{"run_id":"...","target":"owner/repo#N","verifier_verdict":"NON_PASS","adjudicated_verdict":"PASS"}]}.\nVersion 1 accepts run_id and target only; it cannot detect changed verdicts.\nJSON is written to stdout. No replay, dispatch or outcome writes.\n');
      return;
    }
    if (!values.db || !values['observed-at']) {
      throw new Error('--db and --observed-at are required');
    }
    const report = inventory({
      db: values.db, observedAt: Number(values['observed-at']),
      days: Number(values.days), expectedCount: Number(values['expected-count']),
      manifest: values.manifest,
    });
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    if (report.status === 'unavailable') process.exitCode = 2;
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = { inventory };
if (require.main === module) main();
