'use strict';

// Gap-only companion to the read-only inventory exporter. A supplied manifest
// is not authentication, and inventory eligibility is not a shadow result.
// JSON goes to stdout so callers can preserve it without overwriting a report.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { parseArgs } = require('node:util');
const { DatabaseSync } = require('node:sqlite');
const { inventory, ownerExcluded } = require('./export_adjudicator_inventory');

const identityFields = ['run_id', 'target', 'verifier_verdict', 'adjudicated_verdict'];
const identityKey = (row) => JSON.stringify(identityFields.map((key) => row[key]));
const nonblank = (value) => typeof value === 'string' && Boolean(value.trim());

function readSavedReport(file) {
  const bytes = fs.readFileSync(file);
  const report = JSON.parse(bytes);
  if (!Array.isArray(report?.rows) || report.rows.some((row) =>
    !row || identityFields.some((key) => !nonblank(row[key])))) {
    throw new Error('saved report requires rows with complete dispute identities');
  }
  const byIdentity = new Map();
  report.rows.forEach((row, index) => {
    const key = identityKey(row);
    const entries = byIdentity.get(key) || [];
    entries.push({ row, index });
    byIdentity.set(key, entries);
  });
  return { byIdentity, rowCount: report.rows.length,
    reference: { path: path.resolve(file),
      sha256: createHash('sha256').update(bytes).digest('hex') } };
}

function savedCaseEvidence(identity, saved) {
  const matches = saved.byIdentity.get(identityKey(identity)) || [];
  if (matches.length !== 1) return {
    evidence_gaps: [matches.length ? 'saved_case_ambiguous' : 'saved_case_missing'],
  };
  const { row, index } = matches[0];
  const packetTarget = row.packet?.target;
  const packetMatches = packetTarget === identity.target;
  const head = packetMatches ? row.packet?.ground_truth_evidence?.head_sha : undefined;
  const revision = typeof head === 'string' && /^[a-f0-9]{40}$/.test(head) ? head : 'UNKNOWN';
  // A report row is a claim about a role/cost record, not that persisted record.
  // Preserve the claims for recovery, without promoting them to verified fields.
  return {
    saved_report_evidence: {
      status: 'unverified_saved_report_row', report: saved.reference, row_index: index,
      row_sha256: createHash('sha256').update(JSON.stringify(row)).digest('hex'),
      reported_source_revision: revision,
      reported_role_run_id: nonblank(row.role_run_id) ? row.role_run_id : 'UNKNOWN',
      reported_backend_run_id: nonblank(row.backend_run_id) ? row.backend_run_id : 'UNKNOWN',
      reported_decision: nonblank(row.decision) ? row.decision : 'UNKNOWN',
      reported_cost_usd: Number.isFinite(row.cost_usd) && row.cost_usd >= 0
        ? row.cost_usd : 'UNKNOWN',
    },
    evidence_gaps: [
      ...(!nonblank(packetTarget) ? ['saved_packet_target_missing']
        : !packetMatches ? ['saved_packet_target_mismatch'] : []),
      ...(revision === 'UNKNOWN' ? ['source_revision_unavailable'] : []),
      ...(!nonblank(row.role_run_id) ? ['saved_role_run_id_missing'] : []),
      ...(!nonblank(row.backend_run_id) ? ['saved_backend_run_id_missing'] : []),
      ...(row.error || (Array.isArray(row.errors) && row.errors.length) ? ['saved_case_error'] : []),
      'shadow_result_not_verified', 'persisted_role_record_not_verified', 'measured_cost_not_verified',
    ],
  };
}

// A saved role ID is only a lookup hint. Read identity before metadata and
// follow the backend link only after the stored role agrees with the report.
// These records do not bind the original dispute or its evaluated revision.
function persistedRoleEvidence(connection, row) {
  const claim = row.saved_report_evidence;
  if (!claim || claim.reported_role_run_id === 'UNKNOWN') {
    return { evidence_gaps: ['persisted_role_lookup_identity_unavailable'] };
  }
  const identity = connection.prepare('SELECT target FROM runs WHERE run_id=?')
    .get(claim.reported_role_run_id);
  if (!identity) return { evidence_gaps: ['persisted_role_missing'] };
  if (identity.target !== row.target) {
    return { evidence_gaps: ['persisted_role_target_mismatch'] };
  }
  const record = connection.prepare(
    'SELECT run_id,target,role_name,agent,decomposition FROM runs WHERE run_id=?')
    .get(claim.reported_role_run_id);
  if (record.role_name !== 'adjudicator') {
    return { evidence_gaps: ['persisted_role_kind_mismatch'] };
  }
  let metadata;
  try { metadata = JSON.parse(record.decomposition); } catch {
    return { evidence_gaps: ['persisted_role_metadata_invalid'] };
  }
  if (!metadata || metadata.role !== 'adjudicator') {
    return { evidence_gaps: ['persisted_role_metadata_invalid'] };
  }
  if (!nonblank(metadata.backend_run_id) || claim.reported_backend_run_id === 'UNKNOWN' ||
      metadata.backend_run_id !== claim.reported_backend_run_id) {
    return { evidence_gaps: ['persisted_backend_run_id_mismatch'] };
  }
  if (claim.reported_decision === 'UNKNOWN' || metadata.action !== claim.reported_decision) {
    return { evidence_gaps: ['persisted_role_decision_mismatch'] };
  }
  const evidence = {
    status: 'matched_role_record_only',
    role_run_id: record.run_id, target: record.target, agent: record.agent,
    backend_run_id: metadata.backend_run_id, decision: metadata.action,
    record_sha256: createHash('sha256').update(JSON.stringify(record)).digest('hex'),
    source_revision: 'UNKNOWN', original_dispute_binding_verified: false,
  };
  // Do not follow a report/role link into an owner-excluded backend run.
  // Reuse the inventory's owner boundary, without inspecting backend metadata.
  const backend = connection.prepare('SELECT target FROM runs WHERE run_id=?')
    .get(metadata.backend_run_id);
  if (ownerExcluded(backend?.target)) return {
    persisted_role_evidence: evidence,
    evidence_gaps: ['persisted_backend_owner_excluded', 'source_revision_not_bound_to_persisted_role'],
  };
  let cost;
  let costGap;
  try {
    cost = connection.prepare('SELECT run_id,cost_usd,source FROM costs WHERE run_id=?')
      .get(metadata.backend_run_id);
  } catch { costGap = 'persisted_cost_store_unavailable'; }
  // Match feedback.role_cost_report: positive ccusage is a whole-run receipt;
  // zero, ledger proxies and absent telemetry never mean a measured free run.
  const measured = cost?.source === 'ccusage' && Number.isFinite(cost.cost_usd) && cost.cost_usd > 0;
  evidence.backend_cost = {
    status: measured ? 'measured_whole_run_receipt' : 'unknown',
    cost_usd: measured ? cost.cost_usd : 'UNKNOWN',
    ...(cost ? { source: cost.source,
      record_sha256: createHash('sha256').update(JSON.stringify(cost)).digest('hex') } : {}),
  };
  return { persisted_role_evidence: evidence, evidence_gaps: [
    'source_revision_not_bound_to_persisted_role',
    ...(!measured ? [costGap || (cost ? 'persisted_cost_not_measured' : 'persisted_cost_missing')] : []),
    ...(measured && claim.reported_cost_usd !== 'UNKNOWN' &&
      claim.reported_cost_usd !== cost.cost_usd ? ['saved_cost_differs_from_persisted_receipt'] : []),
  ] };
}

function attachPersistedRoles(db, cases) {
  let connection;
  try {
    connection = new DatabaseSync(db, { readOnly: true });
    connection.exec('BEGIN');
    for (const row of cases) {
      if (row.inventory_reasons.includes('owner_excluded_repository')) continue;
      let evidence;
      try { evidence = persistedRoleEvidence(connection, row); } catch {
        evidence = { evidence_gaps: ['persisted_role_store_unavailable'] };
      }
      if (evidence.persisted_role_evidence) {
        row.persisted_role_evidence = evidence.persisted_role_evidence;
        row.evidence_gaps = row.evidence_gaps.filter((gap) => gap !== 'persisted_role_record_not_verified');
      }
      row.evidence_gaps.push(...evidence.evidence_gaps);
    }
  } catch {
    for (const row of cases) {
      if (!row.inventory_reasons.includes('owner_excluded_repository')) {
        row.evidence_gaps.push('persisted_role_store_unavailable');
      }
    }
  } finally {
    if (connection) connection.close();
  }
}

function caseGaps(options) {
  if (!options?.manifest) throw new Error('--manifest is required for per-case gap accounting');
  if (options.persistedRoles && options.report === undefined) {
    throw new Error('--persisted-roles requires --report');
  }
  const snapshot = inventory(options);
  const reconciliation = snapshot.manifest_reconciliation;
  if (options.report !== undefined && reconciliation.identity_scope !== 'dispute') {
    throw new Error('saved report reconciliation requires a schema_version 2 dispute manifest');
  }
  const saved = options.report === undefined ? null : readSavedReport(options.report);
  const cases = reconciliation.cases.map((row) => {
    const { status, reasons, ...identity } = row;
    const excluded = reasons.includes('owner_excluded_repository');
    // Never inspect saved execution evidence for an owner-excluded identity.
    const evidence = saved && !excluded ? savedCaseEvidence(identity, saved) : null;
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
      ...(evidence?.saved_report_evidence ? { saved_report_evidence: evidence.saved_report_evidence } : {}),
      evidence_gaps: excluded
        ? ['owner_excluded_repository']
        : [...reasons, ...(evidence?.evidence_gaps || ['shadow_evidence_not_collected'])],
    };
  });
  if (options.persistedRoles) attachPersistedRoles(options.db, cases);
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
    unsupplied_requested_slots: Math.max(0, snapshot.expected_count - cases.length),
    // Retain manifest digest, observation time, exclusions and discrepancies
    // together with the gaps; do not turn an unavailable store into zero cases.
    inventory_snapshot: snapshot,
    ...(saved ? { saved_report_snapshot: { ...saved.reference, row_count: saved.rowCount,
      matched_case_count: cases.filter((row) => row.saved_report_evidence).length,
      // This count includes excluded rows; it does not inspect their execution fields.
      unmatched_row_count: saved.rowCount - cases.filter((row) => row.saved_report_evidence).length,
      persisted_records_verified: false,
      ...(options.persistedRoles ? { persisted_role_lookup_requested: true } : {}),
    } } : {}),
    cases,
    limitations: [
      'Supplied identities have not been authenticated against the original request.',
      options.persistedRoles
        ? 'Persisted role/backend links are checked only against saved claims; case-level revision, shadow result, provenance and cost fields remain UNKNOWN.'
        : saved
        ? 'Saved report claims are bound to file and row digests only; execution revisions, shadow results, persisted role records and measured costs remain unverified.'
        : 'Only inventory was read; saved shadow results, execution revisions, role runs and costs were not collected.',
      'Owner-excluded repositories require no further inspection; their identities are retained for accounting only.',
      ...(cases.length === 0 ? ['No supplied identities; no per-case operational acceptance is established.'] : []),
      ...(options.persistedRoles ? ['Persisted role matches and backend cost receipts do not establish original dispute membership, execution revision or a verified shadow result.'] : []),
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
      report: { type: 'string' },
      'persisted-roles': { type: 'boolean' },
      help: { type: 'boolean', short: 'h' },
    } });
    if (values.help) {
      process.stdout.write('Usage: node scripts/export_adjudicator_case_gaps.js --db BRAIN --observed-at UNIX_SECONDS --manifest JSON_FILE [--report SAVED_REPORT [--persisted-roles]] [--days 90] [--expected-count 71]\nGap-only JSON to stdout. Optional report requires a schema 2 manifest. --persisted-roles compares its role/backend/decision claims with read-only store records; it cannot bind their execution revision or authenticate the supplied manifest. Does not dispatch or write outcomes.\n');
      return;
    }
    const result = caseGaps({
      db: values.db, observedAt: Number(values['observed-at']),
      days: Number(values.days), expectedCount: Number(values['expected-count']),
      manifest: values.manifest,
      report: values.report,
      persistedRoles: values['persisted-roles'],
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
