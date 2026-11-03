'use strict';

// Read-only snapshot of the Brain selection used by adjudicator_retro.disputes,
// plus explicit exclusions for runs or outcomes missing their matching row.
// Requires Node >=22.13 (built-in SQLite). JSON goes to stdout; callers choose
// where to preserve it. The separate --report mode inventories retained report
// bytes, including recursive paid history, without opening Brain or roles.
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

// These observations describe saved fields, never a freshly validated result.
// Error prose can include private provider material. Bind it by JSON-value hash
// and location so recovery can inspect the original without publishing the text.
function attemptObservation(attempt, caseId, location) {
  const failureFields = ['error', 'errors', 'retry_error', 'role_record_error']
    .filter((field) => Boolean(attempt[field]) &&
      (!Array.isArray(attempt[field]) || attempt[field].length > 0))
    .map((field) => ({ location: `${location}.${field}`,
      json_value_sha256: createHash('sha256').update(JSON.stringify(attempt[field])).digest('hex') }));
  const runFailed = failureFields.some((item) =>
    item.location === `${location}.error` || item.location === `${location}.errors`);
  const binary = ['reject_blocker', 'uphold_blocker'].includes(attempt.decision);
  const abstained = attempt.decision === 'needs_more_evidence';
  let result;
  let nextStep;
  if ((binary || abstained) && runFailed) {
    result = 'conflicting_retained_decision_and_failure';
    nextStep = 'inspect_retained_conflict_before_any_retry';
  } else if (runFailed) {
    result = 'retained_run_failure';
    nextStep = 'inspect_failure_fields_for_first_actual_broken_step';
  } else if (binary) {
    result = 'retained_binary_decision';
    nextStep = 'authenticate_native_receipt_then_refresh_independent_truth';
  } else if (abstained) {
    result = 'retained_abstention';
    nextStep = 'recover_fresh_collected_evidence_before_any_retry';
  } else if (attempt.decision) {
    result = 'unrecognized_retained_decision';
    nextStep = 'inspect_unrecognized_decision_before_any_retry';
  } else {
    result = 'no_retained_decision';
    nextStep = 'recover_attempt_completion_without_redispatch';
  }
  return { case_id: caseId, location, retained_result: result,
    backend_run_id: attempt.backend_run_id ?? null,
    retained_binary_truth_present: ['PASS', 'FAIL'].includes(attempt.later_truth),
    failure_fields: failureFields, next_evidence_step: nextStep };
}

// Observe native receipt locators in one read-only SQLite snapshot. This does
// not instantiate NativeAttemptJournal, parse provider output, authenticate a
// decision, acquire the publication lock, or establish live writer ownership.
function journalInventory(journal) {
  const base = { status: 'unavailable', path: journal ? path.resolve(journal) : null,
    attempts: null, receipt_authority: 'not_authenticated_by_inventory' };
  if (journal === undefined) return { ...base, reason: 'native_journal_not_supplied' };
  if (typeof journal !== 'string' || !journal.trim()) throw new Error('journal path is required');
  let connection;
  try {
    connection = new DatabaseSync(journal, { readOnly: true });
    connection.exec('BEGIN');
    if (connection.prepare('PRAGMA application_id').get().application_id !== 1313097550) {
      return { ...base, reason: 'non_native_journal' };
    }
    const steps = new Map([
      ['reserved', 'producer_must_prove_invocation_never_started'],
      ['in_flight', 'UNKNOWN_recover_provider_receipt_before_any_retry'],
      ['raw_persisted', 'resume_native_validation_without_redispatch'],
      ['validated', 'reauthenticate_with_native_projection_before_grading'],
    ]);
    const attempts = connection.prepare(
      'SELECT attempt_id,case_id,material_revision,case_digest,state,backend_run_id,' +
      'report_sha256,created_at,updated_at,' +
      '(raw_response IS NOT NULL) AS raw_receipt_present,' +
      '(validated_json IS NOT NULL) AS validation_present FROM native_attempts ' +
      'ORDER BY created_at,attempt_id').all().map((item) => ({ ...item,
      raw_receipt_present: Boolean(item.raw_receipt_present),
      validation_present: Boolean(item.validation_present),
      recovery_step: steps.get(item.state) || 'unrecognized_state_requires_native_review',
    }));
    return { ...base, status: 'available', attempts };
  } catch {
    // SQLite messages and raw payloads can contain private provider material.
    return { ...base, reason: 'native_journal_unreadable_or_schema_unavailable' };
  } finally {
    if (connection) connection.close();
  }
}

function preDispatchInventory(saved, paid, journal) {
  const native = journalInventory(journal);
  const identities = new Set(paid.map((item) => item.backend_run_id));
  const journalIdentities = new Set((native.attempts || [])
    .map((item) => item.backend_run_id).filter(Boolean));
  return {
    requested_generic_command: '--dispatch --retry --limit 21',
    generic_retry_route: Object.hasOwn(saved, 'native_publication')
      ? 'refused_native_canonical_publication' : 'not_authorized_by_inventory',
    native_route: 'admit_collected_retry_then_publish_native_cohort',
    native_journal: native,
    paid_identity_reconciliation: {
      report_unique: identities.size,
      journal_unique: native.status === 'available' ? journalIdentities.size : null,
      union_unique: native.status === 'available'
        ? new Set([...identities, ...journalIdentities]).size : null,
      report_only: native.status === 'available'
        ? [...identities].filter((item) => !journalIdentities.has(item)) : null,
      journal_only: native.status === 'available'
        ? [...journalIdentities].filter((item) => !identities.has(item)) : null,
      // A started attempt without an id is still a possible paid invocation.
      unresolved_without_backend_id: native.status === 'available'
        ? native.attempts.filter((item) => item.state === 'in_flight' && !item.backend_run_id)
          .map((item) => item.attempt_id) : null,
    },
    live_writer_ownership: 'unverified',
    dispatch_authorized: false,
    stop_reasons: [
      ...(native.status === 'unavailable' ? [native.reason] : []),
      ...((native.attempts || []).some((item) =>
        ['reserved', 'in_flight', 'raw_persisted'].includes(item.state))
        ? ['unresolved_native_attempts_require_recovery'] : []),
      ...((native.attempts || []).some((item) =>
        !['reserved', 'in_flight', 'raw_persisted', 'validated'].includes(item.state))
        ? ['unrecognized_native_state_requires_review'] : []),
      'live_writer_ownership_unverified', 'accepted_deployed_mirror_bindings_unverified',
      'immutable_source_and_case_qualification_unverified',
      'native_receipt_authentication_and_independent_truth_required',
    ],
  };
}

function reportInventory({ report, observedAt, evidenceClass, evidenceReference, journal, manifest }) {
  integer(observedAt, 'observedAt', 0);
  if (typeof report !== 'string' || !report.trim()) throw new Error('report is required');
  if (!['historical_snapshot', 'current_snapshot'].includes(evidenceClass)) {
    throw new Error('evidenceClass must be historical_snapshot or current_snapshot');
  }
  if (typeof evidenceReference !== 'string' || !evidenceReference.trim()) {
    throw new Error('evidenceReference is required');
  }
  const requestedManifest = manifest === undefined ? null : readManifest(manifest);
  const bytes = fs.readFileSync(report);
  const saved = JSON.parse(bytes);
  if (!saved || !Array.isArray(saved.rows)) throw new Error('report requires a rows array');
  const seen = new Set();
  const paid = [];
  const observations = [];
  const cases = saved.rows.map((row, index) => {
    if (!row || typeof row.case_id !== 'string' || !row.case_id.trim() || !hasTarget(row)) {
      throw new Error(`rows[${index}] requires nonblank case_id and target`);
    }
    if (seen.has(row.case_id)) throw new Error(`duplicate case_id at rows[${index}]`);
    seen.add(row.case_id);
    // Iterative pre-order traversal preserves every retained receipt locator,
    // including repeated paid identities. It never turns retries into cases.
    const stack = [{ attempt: row, location: `rows[${index}]` }];
    while (stack.length) {
      const { attempt, location } = stack.pop();
      if (!attempt || typeof attempt !== 'object' || Array.isArray(attempt)) {
        throw new Error(`invalid attempt at ${location}`);
      }
      if (attempt.backend_run_id !== undefined && attempt.backend_run_id !== null) {
        if (typeof attempt.backend_run_id !== 'string' || !attempt.backend_run_id.trim()) {
          throw new Error(`invalid backend_run_id at ${location}`);
        }
        paid.push({ case_id: row.case_id, backend_run_id: attempt.backend_run_id, location });
      }
      observations.push(attemptObservation(attempt, row.case_id, location));
      const history = attempt.retry_attempts === undefined ? [] : attempt.retry_attempts;
      if (!Array.isArray(history)) throw new Error(`invalid retry_attempts at ${location}`);
      for (let child = history.length - 1; child >= 0; child -= 1) {
        stack.push({ attempt: history[child], location: `${location}.retry_attempts[${child}]` });
      }
    }
    return {
      case_id: row.case_id, target: row.target, location: `rows[${index}]`,
      run_id: row.run_id ?? null,
      verifier_verdict: row.verifier_verdict ?? null,
      adjudicated_verdict: row.adjudicated_verdict ?? null,
      retained_decision: row.decision ?? null,
      retained_later_truth: row.later_truth ?? null,
      retained_error_present: Boolean(row.error || row.errors?.length),
      native_projection_present: Object.hasOwn(row, 'native_projection'),
    };
  });
  const identities = new Map();
  for (const item of paid) {
    const occurrences = identities.get(item.backend_run_id) || [];
    occurrences.push(item.location);
    identities.set(item.backend_run_id, occurrences);
  }
  const preDispatch = preDispatchInventory(saved, paid, journal);
  const reconciliation = requestedManifest
    ? reconcileRetainedRows(cases, requestedManifest, observations) : null;
  if (!fs.readFileSync(report).equals(bytes)) {
    throw new Error('report changed during inventory');
  }
  if (requestedManifest && createHash('sha256').update(fs.readFileSync(manifest)).digest('hex') !==
      requestedManifest.sha256) {
    throw new Error('manifest changed during inventory');
  }
  return {
    schema_version: 1,
    scope: 'retained_report_only', evidence_class: evidenceClass,
    evidence_reference: evidenceReference, observed_at: observedAt,
    report_path: path.resolve(report), report_sha256: createHash('sha256').update(bytes).digest('hex'),
    report_byte_length: bytes.length,
    retained_generated_at: saved.generated_at ?? null,
    retained_population: saved.population ?? null,
    retained_summary: saved.summary ?? null,
    native_publication_present: Object.hasOwn(saved, 'native_publication'),
    cases, case_count: cases.length, paid_attempts: paid,
    retained_attempt_observations: observations,
    manifest_reconciliation: reconciliation,
    pre_dispatch: preDispatch,
    paid_identity_summary: {
      records: paid.length, unique: identities.size,
      repeated: [...identities].filter(([, locations]) => locations.length > 1)
        .map(([backend_run_id, locations]) => ({ backend_run_id, locations })),
    },
    // Retained summaries can be stale or contradictory. No summary, source
    // label, saved native projection or matching count authenticates a grade.
    receipt_authority: 'not_authenticated_by_inventory',
    requested_cohort_verified: false, dispatch_authorized: false,
    evidence_gaps: ['original_cohort_provenance_unverified',
      ...(preDispatch.native_journal.status === 'available'
        ? ['native_journal_receipts_not_authenticated'] : ['native_journal_not_observed']),
      'independent_current_truth_not_observed', 'deployed_and_mirror_revisions_not_observed',
      'fixed_next_20_manifest_not_observed'],
  };
}

// Reconcile identities only. A saved decision/truth pair, native marker, paid
// locator or a perfect manifest match cannot authenticate an actual outcome.
function reconcileRetainedRows(cases, manifest, observations) {
  const exact = (requested, row) => requested.target === row.target &&
    (manifest.schema_version === 1 ||
      (requested.verifier_verdict === row.verifier_verdict &&
       requested.adjudicated_verdict === row.adjudicated_verdict));
  const byRun = new Map();
  for (const row of cases) {
    const rows = byRun.get(row.run_id) || [];
    rows.push(row);
    byRun.set(row.run_id, rows);
  }
  const matchedLocations = new Set();
  const requested = manifest.cases.map((item) => {
    const candidates = byRun.get(item.run_id) || [];
    const matches = candidates.filter((row) => exact(item, row));
    matches.forEach((row) => matchedLocations.add(row.location));
    let status;
    let reasons;
    if (matches.length === 1) {
      status = 'matched'; reasons = [];
    } else if (matches.length > 1) {
      status = 'ambiguous'; reasons = ['multiple_retained_rows_match_requested_identity'];
    } else if (!candidates.length) {
      status = 'missing'; reasons = ['requested_run_missing_from_report'];
    } else {
      const targetMatches = candidates.filter((row) => row.target === item.target);
      if (!targetMatches.length) {
        status = 'identity_mismatch'; reasons = ['requested_target_mismatch'];
      } else if (targetMatches.some((row) =>
        [row.verifier_verdict, row.adjudicated_verdict].some((value) =>
          typeof value !== 'string' || !value.trim()))) {
        status = 'unavailable'; reasons = ['retained_verdict_identity_incomplete'];
      } else {
        status = 'identity_mismatch'; reasons = ['requested_verdict_mismatch'];
      }
    }
    return { ...item, status, reasons,
      // Ambiguous/conflicting rows cannot lend their failures to a requested
      // identity. A unique match still authenticates neither outcome nor truth.
      retained_attempt_locations: matches.length === 1
        ? observations.filter((row) => row.case_id === matches[0].case_id)
          .map((row) => row.location) : [],
      retained_locations: (matches.length ? matches : candidates).map((row) => row.location) };
  });
  return { scope: 'supplied_manifest_against_retained_report_only',
    identity_scope: manifest.schema_version === 2 ? 'dispute' : 'run_target',
    manifest_path: manifest.path, manifest_sha256: manifest.sha256,
    requested_count: requested.length, cases: requested,
    all_requested_rows_uniquely_matched: requested.length > 0 &&
      requested.every((item) => item.status === 'matched'),
    unmatched_retained_locations: cases.filter((row) => !matchedLocations.has(row.location))
      .map((row) => row.location),
    original_cohort_provenance_verified: false, authenticated_outcomes_verified: false };
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
    const { values, tokens } = parseArgs({ tokens: true, options: {
      db: { type: 'string' },
      report: { type: 'string' },
      journal: { type: 'string' },
      'evidence-class': { type: 'string' },
      'evidence-reference': { type: 'string' },
      'observed-at': { type: 'string' },
      days: { type: 'string', default: '90' },
      'expected-count': { type: 'string', default: '71' },
      manifest: { type: 'string' },
      help: { type: 'boolean', short: 'h' },
    } });
    if (values.help) {
      process.stdout.write('Report recovery: node scripts/export_adjudicator_inventory.js --report SAVED_JSON --observed-at UNIX_SECONDS --evidence-class historical_snapshot --evidence-reference SOURCE_REFERENCE [--journal EXISTING_NATIVE_SQLITE] [--manifest JSON_FILE]\nRetained rows, supplied-manifest identity matching and paid identities only; no grade authentication or dispatch.\n');
      process.stdout.write('Usage: node scripts/export_adjudicator_inventory.js --db BRAIN --observed-at UNIX_SECONDS [--days 90] [--expected-count 71] [--manifest JSON_FILE]\nManifest: {"schema_version":2,"cases":[{"run_id":"...","target":"owner/repo#N","verifier_verdict":"NON_PASS","adjudicated_verdict":"PASS"}]}.\nVersion 1 accepts run_id and target only; it cannot detect changed verdicts.\nJSON is written to stdout. No replay, dispatch or outcome writes.\n');
      return;
    }
    if (values.report) {
      if (tokens.some((item) => ['db', 'days', 'expected-count'].includes(item.name))) {
        throw new Error('--report cannot combine with Brain window options');
      }
      const result = reportInventory({
        report: values.report, observedAt: Number(values['observed-at']),
        evidenceClass: values['evidence-class'], evidenceReference: values['evidence-reference'],
        journal: values.journal,
        manifest: values.manifest,
      });
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
      return;
    }
    if (values.journal || values['evidence-class'] || values['evidence-reference']) {
      throw new Error('--journal, --evidence-class and --evidence-reference require --report');
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

module.exports = { inventory, reportInventory };
if (require.main === module) main();
