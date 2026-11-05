'use strict';

// Observe an already fixed manifest against the existing native live report.
// No cohort selection, provider calls, Brain reads or production writes.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { parseArgs } = require('node:util');
const { ownerExcluded } = require('./export_adjudicator_inventory');

const identityFields = ['run_id', 'target', 'verifier_verdict', 'adjudicated_verdict'];
const nonblank = (value) => typeof value === 'string' && Boolean(value.trim());
const key = (row) => JSON.stringify(identityFields.map((field) => row[field]));
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const timestamp = (value) => Number.isSafeInteger(value) && value >= 0 &&
  Number.isFinite(new Date(value * 1000).getTime());
const iso = (value) => new Date(value * 1000).toISOString();

function liveCohort({ manifest, expectedManifestHash, manifestReference, report, observedAt }) {
  if (!nonblank(manifest) || !nonblank(report) || !nonblank(manifestReference)) {
    throw new Error('manifest, report and manifest reference are required');
  }
  if (!/^[a-f0-9]{64}$/.test(expectedManifestHash || '')) {
    throw new Error('pin the fixed manifest with its expected SHA-256');
  }
  if (!timestamp(observedAt)) throw new Error('observedAt must be a valid Unix timestamp');
  const manifestBytes = fs.readFileSync(manifest);
  if (digest(manifestBytes) !== expectedManifestHash) throw new Error('fixed manifest hash mismatch');
  const supplied = JSON.parse(manifestBytes);
  if (supplied?.schema_version !== 2 || !Array.isArray(supplied.cases) ||
      supplied.cases.length !== 20) {
    throw new Error('fixed next-20 requires a schema_version 2 manifest with exactly 20 cases');
  }
  const seen = new Set();
  const identities = supplied.cases.map((row) => {
    if (!row || identityFields.some((field) => !nonblank(row[field])) ||
        row.verifier_verdict === row.adjudicated_verdict) {
      throw new Error('manifest cases require full, disputed verdict identities');
    }
    // Match the native run + verdict-pair identity even across conflicting targets.
    const dispute = JSON.stringify([row.run_id, row.verifier_verdict, row.adjudicated_verdict]);
    if (seen.has(dispute)) throw new Error('duplicate fixed manifest dispute');
    seen.add(dispute);
    return Object.fromEntries(identityFields.map((field) => [field, row[field]]));
  });
  let reportBytes;
  try { reportBytes = fs.readFileSync(report); } catch (error) {
    if (error.code !== 'ENOENT') throw new Error('live report could not be read');
  }
  const saved = reportBytes === undefined ? null : JSON.parse(reportBytes);
  if (reportBytes !== undefined && (saved?.schema !== 'adjudicator-live/v1' ||
      !Array.isArray(saved.rows) || saved.rows.some((row) =>
        !row || typeof row !== 'object' || Array.isArray(row)))) {
    throw new Error('expected the native adjudicator-live/v1 report');
  }
  const rows = saved?.rows || [];
  const byIdentity = new Map();
  const receiptUses = { role_run_id: new Map(), backend_run_id: new Map() };
  for (const [index, row] of rows.entries()) {
    // Exclusions precede any execution evidence access.
    if (ownerExcluded(row.target)) continue;
    const matches = byIdentity.get(key(row)) || [];
    matches.push({ row, index });
    byIdentity.set(key(row), matches);
    for (const [field, uses] of Object.entries(receiptUses)) {
      if (nonblank(row[field])) uses.set(row[field], (uses.get(row[field]) || 0) + 1);
    }
  }
  const cases = identities.map((identity) => {
    const gaps = [];
    const base = { ...identity, checked_at: iso(observedAt),
      status: 'unavailable', retained_report_row: null };
    if (ownerExcluded(identity.target)) return { ...base, gaps: ['owner_excluded_repository'] };
    const matches = byIdentity.get(key(identity)) || [];
    if (!saved || matches.length !== 1) return { ...base, gaps: [!saved
      ? 'live_report_unavailable' : matches.length ? 'ambiguous_live_rows' : 'fixed_member_row_missing'] };
    const { row, index } = matches[0];
    if (row.source !== 'live') gaps.push('source_is_not_live');
    if (!['uphold_blocker', 'reject_blocker'].includes(row.decision)) {
      gaps.push(row.decision === 'needs_more_evidence' ? 'retained_abstention' : 'binary_decision_missing');
    }
    if (row.proposal?.decision !== row.decision) gaps.push('proposal_decision_mismatch');
    if (row.disposition !== undefined && row.disposition !== row.decision) {
      gaps.push('disposition_decision_mismatch');
    }
    if (row.certification?.status !== 'certified' ||
        !/^[a-f0-9]{40}$/.test(row.certification?.evaluated_sha || '')) {
      gaps.push('exact_revision_certification_missing');
    }
    if (!timestamp(saved.generated_at) || saved.generated_at > observedAt ||
        !timestamp(row.attempted_at) || row.attempted_at > saved.generated_at ||
        (row.first_attempted_at !== undefined && (!timestamp(row.first_attempted_at) ||
          row.first_attempted_at > row.attempted_at))) {
      gaps.push('attempt_or_report_date_invalid');
    }
    if (!nonblank(row.role_run_id)) gaps.push('role_receipt_locator_missing');
    else if (receiptUses.role_run_id.get(row.role_run_id) !== 1) gaps.push('role_receipt_locator_reused');
    if (!nonblank(row.backend_run_id)) gaps.push('backend_receipt_locator_missing');
    else if (receiptUses.backend_run_id.get(row.backend_run_id) !== 1) gaps.push('backend_receipt_locator_reused');
    if (row.error || (row.errors !== undefined &&
        (!Array.isArray(row.errors) || row.errors.length))) gaps.push('retained_attempt_error');
    return { ...base, status: gaps.length ? 'blocked' : 'retained_binary_decision', gaps,
      retained_report_row: { location: `rows[${index}]`, sha256: digest(JSON.stringify(row)),
        case_id: nonblank(row.case_id) ? row.case_id : null,
        decision: ['uphold_blocker', 'reject_blocker', 'needs_more_evidence'].includes(row.decision)
          ? row.decision : null,
        attempted_at: timestamp(row.attempted_at) ? iso(row.attempted_at) : null,
        role_run_id: nonblank(row.role_run_id) ? row.role_run_id : null,
        backend_run_id: nonblank(row.backend_run_id) ? row.backend_run_id : null } };
  });
  if (!fs.readFileSync(manifest).equals(manifestBytes)) {
    throw new Error('fixed manifest changed during observation');
  }
  if (reportBytes === undefined ? fs.existsSync(report) : !fs.readFileSync(report).equals(reportBytes)) {
    throw new Error('live report changed during observation');
  }
  return { schema_version: 1, scope: 'supplied_fixed_next_20_live_report_only',
    checked_at: iso(observedAt),
    manifest: { path: path.resolve(manifest), sha256: expectedManifestHash,
      reference: manifestReference },
    report: { path: path.resolve(report), status: saved ? 'available' : 'unavailable',
      sha256: reportBytes === undefined ? null : digest(reportBytes),
      generated_at: timestamp(saved?.generated_at) ? iso(saved.generated_at) : null },
    cases, retained_binary_decisions: cases.filter((row) => row.status === 'retained_binary_decision').length,
    // Pinning supplied bytes prevents drift; it cannot recover original provenance
    // or authenticate the saved role/backend locators, collector or historical date.
    original_cohort_provenance_verified: false, dispatch_receipts_authenticated: false,
    historical_proof_verified: false, operational_acceptance_complete: false,
    dispatch_authorized: false };
}

module.exports = { liveCohort };
if (require.main === module) {
  try {
    const { values } = parseArgs({ options: {
      manifest: { type: 'string' }, 'expected-manifest-hash': { type: 'string' },
      'manifest-reference': { type: 'string' }, report: { type: 'string' },
      'observed-at': { type: 'string' },
    } });
    const result = liveCohort({ manifest: values.manifest,
      expectedManifestHash: values['expected-manifest-hash'],
      manifestReference: values['manifest-reference'], report: values.report,
      observedAt: values['observed-at'] === undefined ? undefined : Number(values['observed-at']) });
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    if (result.report.status === 'unavailable') process.exitCode = 2;
  } catch {
    // Parser errors can include private input contents. Do not echo them.
    process.stderr.write('Live cohort observation failed; check pinned inputs and arguments.\n');
    process.exitCode = 1;
  }
}
