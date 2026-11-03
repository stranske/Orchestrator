'use strict';

// Read-only snapshot of the Brain selection used by adjudicator_retro.disputes.
// Requires Node >=22.13 (built-in SQLite). JSON goes to stdout; callers choose
// where to preserve it. This tool never opens the retrospective report or roles.
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');
const { parseArgs } = require('node:util');

function integer(value, name, minimum) {
  if (!Number.isSafeInteger(value) || value < minimum) {
    throw new Error(`${name} must be a safe integer >= ${minimum}`);
  }
  return value;
}

function inventory({ db, observedAt, days = 90, expectedCount = 71 }) {
  if (typeof db !== 'string' || !db.trim()) throw new Error('db is required');
  integer(observedAt, 'observedAt', 0);
  integer(days, 'days', 1);
  integer(expectedCount, 'expectedCount', 0);
  const cutoff = observedAt - days * 86400;
  integer(cutoff, 'cutoff', 0);
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
    count_delta: null,
    requested_cohort_verified: false,
    cases: null,
    exclusions: null,
    evidence_gaps: [{
      kind: 'requested_cohort_unverified',
      detail: 'This window snapshot is not bound to the original requested case manifest; count equality alone cannot verify the cohort.',
    }],
  };
  let connection;
  let rows;
  try {
    connection = new DatabaseSync(report.database, { readOnly: true });
    // One statement supplies a consistent snapshot for counts and exclusions.
    // SQL computes eligibility with the same null and comparison semantics as
    // the Python reader. LEFT JOIN also accounts for runs lacking outcomes.
    rows = connection.prepare(`
      SELECT r.run_id,r.target,r.ts,o.verifier_verdict,o.adjudicated_verdict,
             o.merged,o.durability,o.durability_checked_ts,
             o.run_id IS NOT NULL AS has_outcome,
             r.ts >= ? AS in_window,
             o.verifier_verdict = o.adjudicated_verdict AS verdicts_agree
      FROM runs r LEFT JOIN outcomes o USING(run_id)
      ORDER BY r.ts DESC,r.run_id
    `).all(cutoff);
  } catch (error) {
    report.evidence_gaps.push({ kind: 'brain_unavailable', detail: error.message });
    return report;
  } finally {
    if (connection) connection.close();
  }
  const cases = [];
  const exclusions = [];
  for (const item of rows) {
    const { has_outcome: hasOutcome, in_window: inWindow,
      verdicts_agree: verdictsAgree, ...row } = item;
    const reasons = [];
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
    count_delta: cases.length - expectedCount, cases, exclusions,
  });
  if (report.count_delta !== 0) {
    report.evidence_gaps.push({
      kind: 'inventory_count_discrepancy',
      detail: `Observed ${cases.length} eligible disputes; requested ${expectedCount}. The identities explaining the difference remain unverified.`,
    });
  }
  return report;
}

function main() {
  try {
    const { values } = parseArgs({ options: {
      db: { type: 'string' },
      'observed-at': { type: 'string' },
      days: { type: 'string', default: '90' },
      'expected-count': { type: 'string', default: '71' },
      help: { type: 'boolean', short: 'h' },
    } });
    if (values.help) {
      process.stdout.write('Usage: node scripts/export_adjudicator_inventory.js --db BRAIN --observed-at UNIX_SECONDS [--days 90] [--expected-count 71]\nJSON is written to stdout. No replay, dispatch or outcome writes.\n');
      return;
    }
    if (!values.db || !values['observed-at']) {
      throw new Error('--db and --observed-at are required');
    }
    const report = inventory({
      db: values.db, observedAt: Number(values['observed-at']),
      days: Number(values.days), expectedCount: Number(values['expected-count']),
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
