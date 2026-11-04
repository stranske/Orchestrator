'use strict';

// Compare retained files only. No Brain access, replay, dispatch or publication.
// Exported role matches remain claims about persisted records, not proof of
// original membership, execution revision or accepted adjudication.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { createHash } = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { parseArgs } = require('node:util');
const { isDeepStrictEqual } = require('node:util');
const { ownerExcluded } = require('./export_adjudicator_inventory');

const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const fields = ['run_id', 'target', 'verifier_verdict', 'adjudicated_verdict'];
const key = (row) => JSON.stringify(fields.map((field) => row[field]));
const nonblank = (value) => typeof value === 'string' && Boolean(value.trim());
const amount = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const excludedAttempt = (row) => ownerExcluded(row.target) ||
  (Array.isArray(row.retry_attempts) && row.retry_attempts.some(excludedAttempt));

function read(file, json = true) {
  const bytes = fs.readFileSync(file);
  return { value: json ? JSON.parse(bytes) : bytes.toString('utf8'),
    reference: { path: path.resolve(file), sha256: digest(bytes) } };
}

function recount(file, python) {
  // Call the actual summary and renderer so their retry/evidence-floor/cost
  // rules cannot drift into a second JavaScript implementation.
  const source = path.resolve(__dirname, '../src');
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-summary-'));
  const stdoutPath = path.join(scratch, 'stdout');
  const stderrPath = path.join(scratch, 'stderr');
  const descriptors = [fs.openSync(stdoutPath, 'w'), fs.openSync(stderrPath, 'w')];
  try {
    const result = spawnSync(python, ['-I', '-B', '-c', `
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import adjudicator_retro as retro
report = json.loads(Path(sys.argv[2]).read_text())
print(json.dumps({"summary": retro.summarize(report["rows"]),
    "weekly_line": retro.weekly_line(Path(sys.argv[2])),
    "interpreter": sys.executable, "python_version": sys.version}))
`, source, path.resolve(file)], { timeout: 30000, stdio: ['ignore', ...descriptors] });
    if (result.error || result.status !== 0) {
      // Do not forward arbitrary source/input content from a traceback.
      throw new Error('selected interpreter could not recompute the saved report');
    }
    return { ...JSON.parse(fs.readFileSync(stdoutPath, 'utf8')),
      source_sha256: Object.fromEntries(['adjudicator_retro.py', 'roles.py', 'feedback.py']
        .map((name) => [name, digest(fs.readFileSync(path.join(source, name)))])) };
  } finally {
    descriptors.forEach((fd) => fs.closeSync(fd));
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

function reconcileSummary(options) {
  const saved = read(options.report);
  const report = saved.value;
  if (!report || typeof report.summary !== 'object' || !report.summary ||
      Array.isArray(report.summary)) throw new Error('report requires a summary object');
  if (report.rows !== undefined && (!Array.isArray(report.rows) || report.rows.some((row) =>
    !row || fields.some((field) => !nonblank(row[field]))))) {
    throw new Error('report rows require complete dispute identities');
  }
  const ledger = options.caseGaps ? read(options.caseGaps) : null;
  if (ledger && (ledger.value?.schema_version !== 1 ||
      ledger.value.scope !== 'supplied_manifest_case_gaps' || !Array.isArray(ledger.value.cases))) {
    throw new Error('case gaps must be a schema 1 supplied_manifest_case_gaps export');
  }
  const output = options.switchReview ? read(options.switchReview, false) : null;
  const gaps = [];
  const add = (kind, extra = {}) => gaps.push({ kind, ...extra });
  if (!ledger) add('persisted_case_export_unavailable');
  if (!output) add('actual_switch_review_output_unavailable');
  if (!Array.isArray(report.rows)) add('saved_case_rows_unavailable');
  if (ledger && ledger.value.saved_report_snapshot?.sha256 !== saved.reference.sha256) {
    add('case_export_report_digest_mismatch');
  }
  const rows = report.rows || [];
  const ledgerRows = ledger?.value.cases || [];
  const byIdentity = new Map();
  for (const row of ledgerRows) {
    const entries = byIdentity.get(key(row)) || [];
    entries.push(row);
    byIdentity.set(key(row), entries);
  }
  const seen = new Set();
  const receipts = new Map();
  const roles = new Set();
  const cases = rows.map((row, index) => {
    const identity = Object.fromEntries(fields.map((field) => [field, row[field]]));
    const caseKey = key(row);
    const matches = byIdentity.get(caseKey) || [];
    const localGaps = [];
    if (seen.has(caseKey)) localGaps.push('duplicate_saved_case_identity');
    seen.add(caseKey);
    // Check exclusions before touching any saved execution evidence.
    if (excludedAttempt(row) || matches.some((item) =>
      item.inventory_reasons?.includes('owner_excluded_repository'))) {
      add('owner_excluded_saved_case', { row_index: index });
      return { ...identity, row_index: index, evidence_gaps: ['owner_excluded_repository'] };
    }
    const candidate = matches.length === 1 ? matches[0] : null;
    const claim = candidate?.saved_report_evidence;
    const bound = ledger?.value.saved_report_snapshot?.sha256 === saved.reference.sha256 &&
      claim?.report?.sha256 === saved.reference.sha256 && claim?.row_index === index &&
      claim?.row_sha256 === digest(JSON.stringify(row));
    if (!bound) localGaps.push(matches.length > 1 ? 'case_export_identity_ambiguous' :
      matches.length === 0 ? 'case_export_row_missing' : 'case_export_row_binding_mismatch');
    const role = bound ? candidate.persisted_role_evidence : null;
    const matchedRole = role?.status === 'matched_role_record_only' &&
      role.target === row.target && role.role_run_id === row.role_run_id &&
      role.backend_run_id === row.backend_run_id && role.decision === row.decision &&
      nonblank(role.role_run_id) && nonblank(role.backend_run_id);
    if (matchedRole) roles.add(role.role_run_id);
    else localGaps.push('persisted_role_not_matched');
    const cost = matchedRole ? role.backend_cost : null;
    const measured = cost?.status === 'measured_whole_run_receipt' && cost.source === 'ccusage' &&
      amount(cost.cost_usd) && cost.cost_usd > 0;
    if (measured) {
      const prior = receipts.get(role.backend_run_id);
      // A shared backend receipt is counted once. Contradictory exports never
      // select a convenient amount; they leave that backend's cost UNKNOWN.
      receipts.set(role.backend_run_id, prior === undefined ? cost.cost_usd :
        prior === cost.cost_usd ? prior : 'UNKNOWN');
      if (!amount(row.cost_usd)) localGaps.push('saved_case_cost_claim_unknown');
      else if (row.cost_usd !== cost.cost_usd) localGaps.push('saved_cost_differs_from_persisted_receipt');
    } else localGaps.push('persisted_cost_unknown');
    if (row.retry_attempts?.length) localGaps.push('retained_attempt_records_not_reconciled');
    localGaps.push('original_dispute_revision_binding_unverified');
    return { ...identity, row_index: index,
      exported_role_record_matched: Boolean(matchedRole),
      exported_backend_run_id: matchedRole ? role.backend_run_id : 'UNKNOWN',
      exported_backend_cost_usd: measured ? cost.cost_usd : 'UNKNOWN',
      evidence_gaps: localGaps };
  });
  const unexpected = ledgerRows.filter((row) => !seen.has(key(row)))
    .map((row) => Object.fromEntries(fields.map((field) => [field, row[field]])));
  if (unexpected.length) add('export_cases_absent_from_saved_report', { cases: unexpected });
  let computed = null;
  const excluded = cases.some((row) => row.evidence_gaps.includes('owner_excluded_repository'));
  if (Array.isArray(report.rows) && !excluded) {
    computed = recount(options.report, options.python || 'python3');
  } else if (excluded) add('summary_recount_withheld_for_owner_exclusion');
  const comparison = computed ? Object.entries(computed.summary).map(([field, value]) => {
    const present = Object.hasOwn(report.summary, field);
    const status = !present ? 'missing_saved_field' :
      isDeepStrictEqual(report.summary[field], value) ? 'match' : 'mismatch';
    if (status !== 'match') add('saved_summary_field_discrepancy', { field, status });
    return { field, saved: present ? report.summary[field] : 'UNKNOWN', recomputed: value, status };
  }) : null;
  const lines = output?.value.split(/\r?\n/).filter((line) => line.startsWith('adjudicator shadow:')) || [];
  const switchStatus = !output ? 'unavailable' : lines.length !== 1 ? 'missing_or_ambiguous_line' :
    !computed ? 'saved_rows_unavailable_or_excluded' :
      lines[0] === computed.weekly_line ? 'matches_saved_renderer' : 'mismatch';
  if (output && switchStatus !== 'matches_saved_renderer') add('switch_review_not_reconciled', { status: switchStatus });
  const conflicts = [...receipts].filter(([, value]) => value === 'UNKNOWN').map(([id]) => id);
  if (conflicts.length) {
    add('conflicting_backend_cost_receipts', { backend_run_ids: conflicts });
    for (const row of cases) if (conflicts.includes(row.exported_backend_run_id)) {
      row.exported_backend_cost_usd = 'UNKNOWN';
      row.evidence_gaps.push('conflicting_backend_cost_receipts');
    }
  }
  for (const input of [saved, ledger, output].filter(Boolean)) {
    if (input.reference.sha256 !== digest(fs.readFileSync(input.reference.path))) {
      throw new Error('input changed during reconciliation');
    }
  }
  return {
    schema_version: 1, scope: 'retained_summary_reconciliation',
    requested_cohort_verified: false, operational_acceptance_complete: false,
    reconciliation_complete: false,
    sources: { saved_report: saved.reference, case_export: ledger?.reference || null,
      switch_review: output?.reference || null,
      reconciler_sha256: digest(fs.readFileSync(__filename)) },
    saved_summary: report.summary,
    saved_population: report.population ?? 'UNKNOWN',
    saved_case_row_count: Array.isArray(report.rows) ? rows.length : null,
    summary_comparison: comparison,
    recount_provenance: computed ? { interpreter: computed.interpreter,
      python_version: computed.python_version, source_sha256: computed.source_sha256 } : null,
    switch_review_comparison: { status: switchStatus, observed_lines: lines,
      expected_saved_line: computed?.weekly_line || 'UNKNOWN',
      persisted_totals_verified: false },
    exported_record_totals: {
      scope: 'current_case_role_matches_only; retry records and original revision binding unverified',
      matched_role_records: roles.size,
      distinct_measured_backends: [...receipts.values()].filter(amount).length,
      known_backend_cost_subtotal_usd: receipts.size && ![...receipts.values()].includes('UNKNOWN')
        ? [...receipts.values()].reduce((sum, value) => sum + value, 0) : null,
      unknown_current_case_costs: Array.isArray(report.rows) ? cases.filter((row) =>
        row.exported_backend_cost_usd === undefined || row.exported_backend_cost_usd === 'UNKNOWN').length : null,
      complete_cost_usd: 'UNKNOWN',
    },
    cases, evidence_gaps: gaps,
  };
}

module.exports = { reconcileSummary };
if (require.main === module) {
  try {
    const { values } = parseArgs({ options: {
      report: { type: 'string' }, 'case-gaps': { type: 'string' },
      'switch-review': { type: 'string' }, python: { type: 'string', default: 'python3' },
    } });
    if (!values.report) throw new Error('--report is required');
    const result = reconcileSummary({ report: values.report, caseGaps: values['case-gaps'],
      switchReview: values['switch-review'], python: values.python });
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    process.exitCode = 2; // A supplied export cannot establish operational acceptance.
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
