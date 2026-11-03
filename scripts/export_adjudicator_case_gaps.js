'use strict';

// Gap-only companion to the read-only inventory exporter. A supplied manifest
// is not authentication, and inventory eligibility is not a shadow result.
// JSON goes to stdout so callers can preserve it without overwriting a report.
const fs = require('node:fs');
const { createHash } = require('node:crypto');
const { parseArgs } = require('node:util');
const { inventory } = require('./export_adjudicator_inventory');

function caseGaps(options) {
  if (!options?.manifest) throw new Error('--manifest is required for per-case gap accounting');
  const snapshot = inventory(options);
  const reconciliation = snapshot.manifest_reconciliation;
  const cases = reconciliation.cases.map((row) => {
    const { status, reasons, ...identity } = row;
    return {
      ...identity,
      status: 'evidence_gap',
      inventory_status: status,
      inventory_reasons: reasons,
      // These describe the uncollected shadow execution, not the exporter.
      source_revision: 'UNKNOWN',
      shadow_result: 'UNKNOWN',
      role_run_provenance: 'UNKNOWN',
      cost_usd: 'UNKNOWN',
      evidence_gaps: reasons.includes('owner_excluded_repository')
        ? ['owner_excluded_repository']
        : [...reasons, 'shadow_evidence_not_collected'],
    };
  });
  return {
    schema_version: 1,
    scope: 'supplied_manifest_case_gaps',
    requested_cohort_verified: false,
    authenticated_requested_case_count: 'UNKNOWN',
    operational_acceptance_complete: false,
    collector_sha256: createHash('sha256').update(fs.readFileSync(__filename)).digest('hex'),
    inventory_collector_sha256: createHash('sha256')
      .update(fs.readFileSync(require.resolve('./export_adjudicator_inventory'))).digest('hex'),
    supplied_case_count: cases.length,
    evidence_gap_count: cases.length,
    unsupplied_requested_slots: snapshot.requested_identity_accounting.unsupplied_requested_slots,
    excess_supplied_cases: snapshot.requested_identity_accounting.excess_supplied_cases,
    // Retain manifest digest, observation time, exclusions and discrepancies
    // together with the gaps; do not turn an unavailable store into zero cases.
    inventory_snapshot: snapshot,
    cases,
    limitations: [
      'Supplied identities have not been authenticated against the original request.',
      'Only inventory was read; saved shadow results, execution revisions, role runs and costs were not collected.',
      'Owner-excluded repositories require no further inspection; their identities are retained for accounting only.',
      ...(cases.length === 0 ? ['No supplied identities; no per-case operational acceptance is established.'] : []),
    ],
  };
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
      process.stdout.write('Usage: node scripts/export_adjudicator_case_gaps.js --db BRAIN --observed-at UNIX_SECONDS --manifest JSON_FILE [--days 90] [--expected-count 71]\nGap-only JSON to stdout. Does not dispatch, inspect shadow roles/costs, or authenticate the supplied manifest.\n');
      return;
    }
    const result = caseGaps({
      db: values.db, observedAt: Number(values['observed-at']),
      days: Number(values.days), expectedCount: Number(values['expected-count']),
      manifest: values.manifest,
    });
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    if (result.inventory_snapshot.status === 'unavailable') process.exitCode = 2;
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = { caseGaps };
if (require.main === module) main();
