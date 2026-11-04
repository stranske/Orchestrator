'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { test } = require('node:test');
const { inventory, reportInventory } = require('../scripts/export_adjudicator_inventory');

const repo = path.resolve(__dirname, '..');
const script = path.join(repo, 'scripts/export_adjudicator_inventory.js');
const now = 2000000000;
const cutoff = now - 90 * 86400;

function retainedReport(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-retained-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const report = path.join(root, 'historical.json');
  const saved = { generated_at: now - 86400, population: 40,
    summary: { cases: 58, graded: 1, advice_graded: 2 },
    native_publication: { source_report_sha256: 'retained-claim' },
    rows: [
      { case_id: 'case-a', target: 'owner/repo#1', decision: 'needs_more_evidence',
        backend_run_id: 'primary', raw_output: 'private backend response',
        error: 'private error detail', native_projection: { graded: 29 },
        retry_attempts: [{ backend_run_id: 'prior',
          retry_attempts: [{ backend_run_id: 'oldest' }] }] },
      { case_id: 'case-b', target: 'owner/repo#2', decision: 'reject_blocker',
        later_truth: 'PASS', backend_run_id: 'prior' },
    ] };
  fs.writeFileSync(report, JSON.stringify(saved));
  return { root, saved, options: { report, observedAt: now,
    evidenceClass: 'historical_snapshot', evidenceReference: 'retained-source-reference' } };
}

function completePacket() {
  return { metadata_only: true, ground_truth_evidence: {
    diff_patch: { revision: 'evaluated-merge', text: 'private diff contents' },
    acceptance_criteria: [{ revision: 'issue-revision', text: 'private criterion text' }],
  } };
}

test('packet floor diagnostics match the production rule without trusting saved flags', (t) => {
  const { root, saved, options } = retainedReport(t);
  const packets = [undefined, null, 'invalid', [], {}, { ground_truth_evidence: [] },
    { ground_truth_evidence: {} }, completePacket()];
  for (const [field, values] of [
    ['diff_patch', [undefined, null, [], {}, { text: 'patch' },
      { revision: 'head', text: ' ' }, { revision: ' ', text: 'patch' }]],
    ['acceptance_criteria', [undefined, null, {}, [], [null], ['criterion'], [{}],
      [{ revision: 'head', text: ' ' }], [{ revision: ' ', text: 'criterion' }]]],
  ]) {
    for (const value of values) {
      const packet = completePacket();
      packet.ground_truth_evidence[field] = value;
      packets.push(packet);
    }
  }
  for (const truncated of [true, false, 1, 0, '', 'false', [], {}, [false]]) {
    const packet = completePacket();
    packet.ground_truth_evidence.diff_patch.truncated = truncated;
    packets.push(packet);
  }
  for (const qualification of [null, false, {}, { persisted_admission: true }]) {
    const packet = completePacket();
    packet.ground_truth_evidence.qualification = qualification;
    packets.push(packet);
  }
  saved.rows = packets.map((packet, index) => ({ case_id: `case-${index}`,
    target: `owner/repo#${index}`, packet, metadata_only: false,
    native_projection: { graded: 29 }, decision: 'reject_blocker', later_truth: 'PASS' }));
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const reference = execute(root, process.env.PYTHON || 'python3', ['-c', `
import json, sys
import adjudicator_retro as retro
with open(sys.argv[1]) as stream:
    rows = json.load(stream)["rows"]
print(json.dumps([retro._row_metadata_only(row) for row in rows]))
`, options.report]);
  assert.equal(reference.status, 0, reference.stderr);
  const result = reportInventory(options);
  assert.deepEqual(result.retained_attempt_observations.map((row) =>
    row.packet_evidence_observation.floor_applies), JSON.parse(reference.stdout));
  assert.equal(result.retained_attempt_observations[7].packet_evidence_observation.status,
    'complete_packet_unverified');
  for (const row of result.retained_attempt_observations) {
    assert.equal(row.packet_evidence_observation.native_admission_verified, false);
    assert.equal(row.packet_evidence_observation.floor_applies,
      row.packet_evidence_observation.blocking_reasons.length > 0);
  }
  assert.equal(result.dispatch_authorized, false);
  assert.equal(Object.hasOwn(result, 'graded'), false);
  assert.ok(!JSON.stringify(result).includes('private'));
});

test('packet diagnostics identify missing and clipped evidence separately on parent and retries', (t) => {
  const { root, saved, options } = retainedReport(t);
  const clipped = completePacket();
  clipped.ground_truth_evidence.diff_patch.truncated = true;
  const counts = { metadata_only: false, ground_truth_evidence: {
    diff_summary: 'private counts', gate_runs: ['private status'],
  } };
  saved.rows = [{ case_id: 'case-a', target: 'owner/repo#1', packet: completePacket(),
    retry_attempts: [{ packet: clipped, retry_attempts: [{ packet: counts }] }, {}] }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const before = fs.readFileSync(options.report);
  const files = fs.readdirSync(root);
  const result = reportInventory(options);
  const observed = result.retained_attempt_observations.map((row) => row.packet_evidence_observation);
  assert.deepEqual(observed.map((row) => row.blocking_reasons.map((reason) => reason.code)), [
    [], ['diff_patch_truncated'], ['diff_patch_not_recorded', 'acceptance_criteria_missing_or_invalid'],
    ['packet_missing_or_invalid'],
  ]);
  assert.equal(observed[1].blocking_reasons[0].location,
    'rows[0].retry_attempts[0].packet.ground_truth_evidence.diff_patch.truncated');
  assert.equal(observed[0].location, 'rows[0].packet');
  assert.equal(observed[0].json_value_sha256,
    createHash('sha256').update(JSON.stringify(saved.rows[0].packet)).digest('hex'));
  assert.equal(observed[3].json_value_sha256, null);
  assert.equal(observed[2].next_evidence_step, 'recover_packet_evidence_before_any_retry');
  assert.equal(observed[0].next_evidence_step, 'authenticate_native_admission_without_redispatch');
  assert.deepEqual(fs.readFileSync(options.report), before);
  assert.deepEqual(fs.readdirSync(root), files);
  assert.ok(!JSON.stringify(result).includes('private'));
});

test('report CLI carries packet floor locators through unique requested-row reconciliation', (t) => {
  const { root, saved, options } = retainedReport(t);
  const identity = { run_id: 'original', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  const packet = completePacket();
  packet.ground_truth_evidence.qualification = { persisted_admission: true };
  saved.rows = [{ ...identity, case_id: 'original-case', packet }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity], 2);
  const result = execute(root, process.execPath, [script, '--report', options.report,
    '--observed-at', String(now), '--evidence-class', options.evidenceClass,
    '--evidence-reference', options.evidenceReference, '--manifest', manifest]);
  assert.equal(result.status, 0, result.stderr);
  const report = JSON.parse(result.stdout);
  const observation = report.retained_attempt_observations[0].packet_evidence_observation;
  assert.deepEqual(observation.blocking_reasons, [{ code: 'serialized_qualification_not_native_admission',
    location: 'rows[0].packet.ground_truth_evidence.qualification' }]);
  assert.deepEqual(report.manifest_reconciliation.cases[0].retained_attempt_locations, ['rows[0]']);
  assert.equal(report.manifest_reconciliation.authenticated_outcomes_verified, false);
  assert.equal(report.requested_cohort_verified, false);
  assert.equal(report.dispatch_authorized, false);
});

test('report recovery preserves ordered rows, recursive paid history and conflicting counts', (t) => {
  const { root, options } = retainedReport(t);
  const before = fs.readFileSync(options.report);
  const files = fs.readdirSync(root);
  const result = reportInventory(options);
  assert.deepEqual(result.cases.map(({ case_id }) => case_id), ['case-a', 'case-b']);
  assert.equal(result.retained_population, 40);
  assert.equal(result.case_count, 2, 'derive rows from evidence, never retained population/summary');
  assert.deepEqual(result.retained_summary, { cases: 58, graded: 1, advice_graded: 2 });
  assert.equal(result.report_sha256, createHash('sha256').update(before).digest('hex'));
  assert.equal(result.report_byte_length, before.length);
  assert.equal(result.retained_generated_at, now - 86400);
  assert.equal(result.observed_at, now, 'recovery time is separate from the retained observation');
  assert.deepEqual(result.paid_attempts.map((item) => item.backend_run_id),
    ['primary', 'prior', 'oldest', 'prior']);
  assert.deepEqual(result.paid_identity_summary, { records: 4, unique: 3, repeated: [
    { backend_run_id: 'prior', locations: ['rows[0].retry_attempts[0]', 'rows[1]'] },
  ] });
  assert.equal(result.cases[0].retained_error_present, true);
  assert.equal(result.cases[1].retained_error_present, false);
  assert.equal(result.cases[1].retained_later_truth, 'PASS');
  assert.equal(result.native_publication_present, true);
  assert.equal(result.cases[0].native_projection_present, true);
  assert.equal(result.receipt_authority, 'not_authenticated_by_inventory');
  assert.equal(result.requested_cohort_verified, false);
  assert.equal(result.dispatch_authorized, false);
  assert.equal(Object.hasOwn(result, 'graded'), false, 'saved claims cannot create fresh grades');
  assert.ok(result.evidence_gaps.includes('native_journal_not_observed'));
  assert.ok(!JSON.stringify(result).includes('private'));
  assert.deepEqual(fs.readFileSync(options.report), before);
  assert.deepEqual(fs.readdirSync(root), files, 'inventory creates no runtime files');
});

test('report recovery rejects missing input without initializing runtime state', (t) => {
  const { root, options } = retainedReport(t);
  const missing = path.join(root, 'missing', 'report.json');
  assert.throws(() => reportInventory({ ...options, report: missing }), /ENOENT/);
  assert.equal(fs.existsSync(path.dirname(missing)), false);
});

test('attempt observations bind nested failure fields without exposing private error text', (t) => {
  const { root, saved, options } = retainedReport(t);
  saved.rows[0].errors = ['private validation error', { detail: 'private backend detail' }];
  saved.rows[0].retry_error = 'private retry failure';
  saved.rows[0].role_record_error = 'private persistence failure';
  saved.rows[0].retry_attempts[0].error = 'private earlier failure';
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const before = fs.readFileSync(options.report);
  const files = fs.readdirSync(root);
  const result = reportInventory(options);
  const observed = result.retained_attempt_observations;
  assert.deepEqual(observed.map((row) => row.location), [
    'rows[0]', 'rows[0].retry_attempts[0]',
    'rows[0].retry_attempts[0].retry_attempts[0]', 'rows[1]',
  ]);
  assert.deepEqual(observed.map((row) => row.backend_run_id),
    ['primary', 'prior', 'oldest', 'prior']);
  for (const [field, index] of [['error', 0], ['errors', 1],
    ['retry_error', 2], ['role_record_error', 3]]) {
    assert.deepEqual(observed[0].failure_fields[index], {
      location: `rows[0].${field}`,
      json_value_sha256: createHash('sha256').update(JSON.stringify(saved.rows[0][field])).digest('hex'),
    });
  }
  assert.deepEqual(observed[1].failure_fields, [{
    location: 'rows[0].retry_attempts[0].error',
    json_value_sha256: createHash('sha256')
      .update(JSON.stringify(saved.rows[0].retry_attempts[0].error)).digest('hex'),
  }]);
  assert.equal(observed[0].retained_result, 'conflicting_retained_decision_and_failure');
  assert.equal(observed[1].retained_result, 'retained_run_failure');
  assert.equal(observed[1].next_evidence_step, 'inspect_failure_fields_for_first_actual_broken_step');
  assert.equal(observed[2].retained_result, 'no_retained_decision');
  assert.equal(observed[3].retained_result, 'retained_binary_decision');
  assert.equal(result.dispatch_authorized, false);
  assert.equal(result.receipt_authority, 'not_authenticated_by_inventory');
  assert.ok(!JSON.stringify(result).includes('private'));
  assert.deepEqual(fs.readFileSync(options.report), before);
  assert.deepEqual(fs.readdirSync(root), files);
});

test('saved decisions survive failed retries and remain distinct from independent truth', (t) => {
  const { saved, options } = retainedReport(t);
  saved.rows = [
    { decision: 'uphold_blocker', later_truth: null,
      retry_error: 'private retry failure', role_record_error: 'private Brain failure',
      retry_attempts: [{ error: 'private failed retry', backend_run_id: 'paid-retry' }] },
    { decision: 'reject_blocker', later_truth: 'PASS', errors: [], error: null },
    { decision: 'needs_more_evidence' },
    { decision: 'unrecognized-private-value', later_truth: 'UNKNOWN' },
    {},
  ].map((row, i) => ({ ...row, case_id: `case-${i}`, target: `owner/repo#${i + 1}` }));
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const result = reportInventory(options);
  const observed = result.retained_attempt_observations;
  assert.deepEqual(observed.map((row) => row.retained_result), [
    'retained_binary_decision', 'retained_run_failure', 'retained_binary_decision',
    'retained_abstention', 'unrecognized_retained_decision', 'no_retained_decision',
  ]);
  assert.deepEqual(observed.map((row) => row.retained_binary_truth_present),
    [false, false, true, false, false, false]);
  assert.equal(observed[0].failure_fields.length, 2, 'persistence and retry failures stay visible');
  assert.equal(observed[0].next_evidence_step,
    'authenticate_native_receipt_then_refresh_independent_truth');
  assert.equal(observed[3].next_evidence_step, 'recover_fresh_collected_evidence_before_any_retry');
  assert.equal(observed[4].next_evidence_step, 'inspect_unrecognized_decision_before_any_retry');
  assert.equal(observed[5].next_evidence_step, 'recover_attempt_completion_without_redispatch');
  assert.equal(result.cases[0].retained_decision, 'uphold_blocker');
  assert.equal(result.cases[0].retained_later_truth, null);
  assert.equal(Object.hasOwn(result, 'graded'), false);
});

test('failure field hashes detect changed evidence even when retained classifications match', (t) => {
  const { saved, options } = retainedReport(t);
  const original = reportInventory(options);
  saved.rows[0].error = 'different private failure';
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const changed = reportInventory(options);
  assert.equal(original.retained_attempt_observations[0].retained_result,
    changed.retained_attempt_observations[0].retained_result);
  assert.notEqual(original.retained_attempt_observations[0].failure_fields[0].json_value_sha256,
    changed.retained_attempt_observations[0].failure_fields[0].json_value_sha256);
  assert.notEqual(original.report_sha256, changed.report_sha256);
  assert.ok(!JSON.stringify(changed).includes('private'));
});

test('later-truth diagnostics distinguish absence, UNKNOWN, binary claims and invalid values', (t) => {
  const { saved, options } = retainedReport(t);
  const attempts = [{}, { later_truth: null }, { later_truth: 'UNKNOWN' },
    { later_truth: 'PASS' }, { later_truth: 'FAIL' }, { later_truth: 'PASS ' },
    { later_truth: true }, { later_truth: [] }, { later_truth: { note: 'private detail' } }];
  saved.rows = attempts.map((row, i) => ({ ...row, case_id: `case-${i}`,
    target: `owner/repo#${i}`, decision: 'reject_blocker', backend_run_id: `paid-${i}` }));
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const before = fs.readFileSync(options.report);
  const result = reportInventory(options);
  const truth = result.retained_attempt_observations.map((row) => row.later_truth_observation);
  assert.deepEqual(truth.map((item) => item.status), [
    'not_recorded', 'not_recorded', 'unknown', 'binary_unverified', 'binary_unverified',
    'invalid', 'invalid', 'invalid', 'invalid',
  ]);
  assert.equal(truth[0].json_value_sha256, null, 'absent is distinct from an explicit null');
  for (const [i, item] of truth.entries()) {
    assert.equal(item.location, `rows[${i}].later_truth`);
    assert.equal(item.independent_truth_verified, false);
    if (i > 0) assert.equal(item.json_value_sha256, createHash('sha256')
      .update(JSON.stringify(attempts[i].later_truth)).digest('hex'));
  }
  assert.equal(truth[0].next_evidence_step, 'recover_independent_later_truth_without_redispatch');
  assert.equal(truth[2].next_evidence_step, 'preserve_UNKNOWN_until_independent_truth_available');
  assert.equal(truth[3].next_evidence_step, 'authenticate_independent_later_truth');
  assert.equal(truth[5].next_evidence_step, 'inspect_invalid_retained_truth_before_grading');
  assert.ok(!JSON.stringify(truth).includes('private detail'));
  assert.equal(result.paid_attempts.length, attempts.length);
  assert.ok(result.cases.every((row) => row.retained_decision === 'reject_blocker'));
  assert.equal(Object.hasOwn(result, 'graded'), false);
  assert.equal(result.dispatch_authorized, false);
  assert.deepEqual(fs.readFileSync(options.report), before);
});

test('parent and retry truth remain separate without deriving truth from a decision', (t) => {
  const { saved, options } = retainedReport(t);
  saved.rows = [{ case_id: 'case-a', target: 'owner/repo#1', decision: 'uphold_blocker',
    later_truth: 'UNKNOWN', backend_run_id: 'parent-paid', retry_attempts: [
      { decision: 'reject_blocker', later_truth: 'PASS', backend_run_id: 'earlier-paid' },
      { decision: 'uphold_blocker' },
    ] }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const result = reportInventory(options);
  assert.deepEqual(result.retained_attempt_observations.map((row) => [
    row.later_truth_observation.status, row.later_truth_observation.location,
  ]), [
    ['unknown', 'rows[0].later_truth'],
    ['binary_unverified', 'rows[0].retry_attempts[0].later_truth'],
    ['not_recorded', 'rows[0].retry_attempts[1].later_truth'],
  ]);
  assert.equal(result.cases[0].retained_later_truth, 'UNKNOWN');
  assert.deepEqual(result.paid_attempts.map((row) => row.backend_run_id), ['parent-paid', 'earlier-paid']);
  assert.equal(result.receipt_authority, 'not_authenticated_by_inventory');
});

test('changed binary truth changes its evidence hash without authenticating either value', (t) => {
  const { saved, options } = retainedReport(t);
  const first = reportInventory(options).retained_attempt_observations[3].later_truth_observation;
  saved.rows[1].later_truth = 'FAIL';
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const changed = reportInventory(options).retained_attempt_observations[3].later_truth_observation;
  assert.equal(first.status, 'binary_unverified');
  assert.equal(changed.status, first.status);
  assert.notEqual(changed.json_value_sha256, first.json_value_sha256);
  assert.equal(changed.independent_truth_verified, false);
});

test('recorded failure stages explain collection, execution and validation breaks on real message shapes', (t) => {
  const { saved, options } = retainedReport(t);
  const attempts = [
    { error: 'current merge-bound verifier decision missing or changed' },
    { errors: ['backend exit=124 timed out after 180s; agy log tail captured',
      'could not parse a JSON adjudication proposal from the backend output'] },
    { errors: ['could not parse a JSON adjudication proposal from the backend output'] },
    { errors: ['recommended_next_step must not contain mutating execution instructions'] },
    { errors: ['no eligible backend has capacity for the adjudicator role'] },
    { error: 'packet requires merged diff summary' },
  ];
  saved.rows = attempts.map((row, i) => ({ ...row, case_id: `case-${i}`, target: `owner/repo#${i}` }));
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const result = reportInventory(options);
  assert.deepEqual(result.retained_attempt_observations.map((row) => row.first_broken_step.stage),
    ['evidence_collection', 'backend_execution', 'proposal_parsing', 'proposal_validation',
      'backend_selection', 'packet_validation']);
  assert.ok(result.retained_attempt_observations.every((row) => row.first_broken_step.status === 'recorded'));
  assert.deepEqual(result.retained_attempt_observations[1].first_broken_step.evidence_locations,
    ['rows[1].errors[0]'], 'backend failure is earlier than its consequent parse failure');
  assert.equal(result.receipt_authority, 'not_authenticated_by_inventory');
  assert.equal(result.dispatch_authorized, false);
});

test('unknown and contradictory failure prose cannot borrow a recognized stage', (t) => {
  const { saved, options } = retainedReport(t);
  const attempts = [
    { error: 'private timeout mentioned in a later note' },
    { errors: ['private unknown failure',
      'could not parse a JSON adjudication proposal from the backend output'] },
    { errors: ['backend exit=0 private timeout note'] },
    { error: { detail: 'packet requires merged diff summary' } },
    { errors: 'could not parse a JSON adjudication proposal from the backend output' },
    { decision: 'reject_blocker', errors: ['backend exit=124 timed out after 180s'] },
  ];
  saved.rows = attempts.map((row, i) => ({ ...row, case_id: `case-${i}`, target: `owner/repo#${i}` }));
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const result = reportInventory(options);
  for (const row of result.retained_attempt_observations) {
    assert.equal(row.first_broken_step.status, 'unresolved');
    assert.equal(row.first_broken_step.stage, null);
    assert.ok(row.first_broken_step.evidence_locations.length > 0);
  }
  assert.ok(!JSON.stringify(result).includes('private'));
});

const retainedRefusals = [
  ['native backend unavailable after terminal timeout; preserving original paid rows, no repeated failed-provider dispatch',
    'failed_provider_guard_refused_dispatch'],
  ['native round boundary before new evidence/provider attempt',
    'round_boundary_refused_dispatch'],
];

test('exact retained launcher refusals explain admission without claiming a backend attempt', (t) => {
  const { saved, options } = retainedReport(t);
  saved.rows = retainedRefusals.map(([error], i) => ({
    case_id: `case-${i}`, target: `owner/repo#${i}`, error,
  }));
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const before = fs.readFileSync(options.report);
  const result = reportInventory(options);
  for (const [i, row] of result.retained_attempt_observations.entries()) {
    assert.equal(row.first_broken_step.status, 'recorded');
    assert.equal(row.first_broken_step.stage, 'dispatch_admission');
    assert.equal(row.first_broken_step.code, retainedRefusals[i][1]);
    assert.equal(row.first_broken_step.message_origin, 'retained_launcher');
    assert.deepEqual(row.first_broken_step.evidence_locations, [`rows[${i}].error`]);
    assert.equal(row.failure_fields[0].json_value_sha256,
      createHash('sha256').update(JSON.stringify(saved.rows[i].error)).digest('hex'));
    assert.equal(row.backend_run_id, null, 'refusal prose cannot supply a paid receipt');
  }
  assert.deepEqual(result.paid_attempts, []);
  assert.equal(result.dispatch_authorized, false);
  assert.equal(result.requested_cohort_verified, false);
  assert.equal(result.receipt_authority, 'not_authenticated_by_inventory');
  assert.deepEqual(fs.readFileSync(options.report), before);
});

test('launcher refusal matching rejects changed prose and contradictory attempt fields', (t) => {
  const { saved, options } = retainedReport(t);
  const attempts = retainedRefusals.flatMap(([message]) => [
    { error: `earlier note: ${message}` },
    { error: `${message} private suffix` },
    { error: message.replace('native ', '') },
    { error: message, errors: ['backend exit=124 timed out after 180s'] },
    { error: message, errors: ['private unknown error'] },
    { error: message, decision: 'uphold_blocker' },
  ]);
  attempts.push({ error: retainedRefusals[0][0], errors: [retainedRefusals[1][0]] });
  saved.rows = attempts.map((row, i) => ({
    ...row, case_id: `case-${i}`, target: `owner/repo#${i}`,
  }));
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const result = reportInventory(options);
  for (const row of result.retained_attempt_observations) {
    assert.equal(row.first_broken_step.status, 'unresolved');
    assert.equal(row.first_broken_step.stage, null);
  }
  assert.ok(!JSON.stringify(result).includes('private'));
});

test('later launcher refusals preserve the parent decision and prior paid history', (t) => {
  const { saved, options } = retainedReport(t);
  saved.rows = [{ case_id: 'case-a', target: 'owner/repo#1', decision: 'uphold_blocker',
    backend_run_id: 'parent-paid', retry_error: retainedRefusals[0][0], retry_attempts: [{
      backend_run_id: 'earlier-paid', errors: ['backend exit=124 timed out after 180s'],
    }] }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const result = reportInventory(options);
  const [parent, prior] = result.retained_attempt_observations;
  assert.equal(parent.retained_result, 'retained_binary_decision');
  assert.equal(parent.first_broken_step.status, 'not_recorded');
  assert.equal(parent.later_retry_broken_step.stage, 'dispatch_admission');
  assert.equal(parent.later_retry_broken_step.message_origin, 'retained_launcher');
  assert.deepEqual(parent.later_retry_broken_step.evidence_locations, ['rows[0].retry_error']);
  assert.equal(prior.first_broken_step.stage, 'backend_execution');
  assert.equal(prior.first_broken_step.message_origin, 'repository_emitter');
  assert.deepEqual(result.paid_attempts.map((row) => row.backend_run_id),
    ['parent-paid', 'earlier-paid']);
  assert.equal(result.cases[0].retained_decision, 'uphold_blocker');
});

test('later retries and persistence breaks cannot replace the parent attempt failure or decision', (t) => {
  const { saved, options } = retainedReport(t);
  saved.rows = [{ case_id: 'case-a', target: 'owner/repo#1', decision: 'uphold_blocker',
    retry_error: 'current merge-bound verifier decision missing or changed',
    role_record_error: 'private database failure', retry_attempts: [{
      errors: ['backend exit=124 timed out after 180s'], backend_run_id: 'paid-retry',
    }] }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const result = reportInventory(options);
  const [parent, retry] = result.retained_attempt_observations;
  assert.equal(parent.retained_result, 'retained_binary_decision');
  assert.equal(parent.first_broken_step.stage, 'role_persistence');
  assert.deepEqual(parent.first_broken_step.evidence_locations, ['rows[0].role_record_error']);
  assert.equal(parent.later_retry_broken_step.stage, 'evidence_collection');
  assert.deepEqual(parent.later_retry_broken_step.evidence_locations, ['rows[0].retry_error']);
  assert.equal(retry.first_broken_step.stage, 'backend_execution');
  assert.equal(retry.later_retry_broken_step, null);
  saved.rows[0].role_record_error = null;
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const cleanParent = reportInventory(options).retained_attempt_observations[0];
  assert.equal(cleanParent.first_broken_step.status, 'not_recorded');
  assert.equal(cleanParent.later_retry_broken_step.status, 'recorded');
  assert.ok(!JSON.stringify(result).includes('private'));
});

test('report recovery refuses malformed and ambiguous retained identities', (t) => {
  const { saved, options } = retainedReport(t);
  for (const change of [
    (value) => { value.rows = {}; },
    (value) => { value.rows[0] = null; },
    (value) => { value.rows[1].case_id = 'case-a'; },
    (value) => { value.rows[0].target = ''; },
    (value) => { value.rows[0].retry_attempts = {}; },
    (value) => { value.rows[0].retry_attempts[0].retry_attempts = [null]; },
    (value) => { value.rows[0].backend_run_id = {}; },
  ]) {
    const malformed = structuredClone(saved);
    change(malformed);
    fs.writeFileSync(options.report, JSON.stringify(malformed));
    assert.throws(() => reportInventory(options), /rows|case_id|retry_attempts|attempt|backend/);
  }
});

test('nested retries cannot borrow a root identity when their saved identities conflict', (t) => {
  const { saved, options } = retainedReport(t);
  const identity = { case_id: 'case-a', run_id: 'original-run', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  for (const field of Object.keys(identity)) {
    for (const nested of [false, true]) {
      const original = structuredClone(saved);
      const root = Object.assign(original.rows[0], identity);
      const retry = { ...identity, [field]: 'private conflicting identity',
        decision: 'reject_blocker', later_truth: 'PASS', backend_run_id: 'primary' };
      root.retry_attempts = nested ? [{ retry_attempts: [retry] }] : [retry];
      const bytes = JSON.stringify(original);
      fs.writeFileSync(options.report, bytes);
      const location = nested ? 'rows[0].retry_attempts[0].retry_attempts[0]'
        : 'rows[0].retry_attempts[0]';
      assert.throws(() => reportInventory(options), (error) => {
        assert.equal(error.message, `invalid retained retry identity at ${location}.${field}`);
        assert.ok(!error.message.includes('private'));
        return true;
      });
      assert.equal(fs.readFileSync(options.report, 'utf8'), bytes);
    }
  }
});

test('explicit retry identities must be verifiable while compact histories remain intact', (t) => {
  const { root, saved, options } = retainedReport(t);
  const identity = { case_id: 'case-a', run_id: 'original-run', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  Object.assign(saved.rows[0], identity);
  saved.rows[0].retry_attempts = [{ ...identity, backend_run_id: 'paid-earlier',
    retry_attempts: [{ backend_run_id: 'paid-oldest' }] }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity], 2);
  const before = fs.readFileSync(options.report);
  const result = reportInventory({ ...options, manifest });
  assert.deepEqual(result.manifest_reconciliation.cases[0].retained_attempt_locations,
    ['rows[0]', 'rows[0].retry_attempts[0]', 'rows[0].retry_attempts[0].retry_attempts[0]']);
  assert.deepEqual(result.paid_attempts.map((row) => row.backend_run_id),
    ['primary', 'paid-earlier', 'paid-oldest', 'prior']);
  assert.deepEqual(fs.readFileSync(options.report), before);
  assert.equal(result.manifest_reconciliation.authenticated_outcomes_verified, false);
  assert.equal(result.dispatch_authorized, false);

  for (const value of [null, '', ' ', 12, {}, [], 'private unbound run']) {
    const changed = structuredClone(saved);
    // Even a valid child run cannot establish its root's missing run identity.
    delete changed.rows[0].run_id;
    changed.rows[0].retry_attempts[0].run_id = value;
    fs.writeFileSync(options.report, JSON.stringify(changed));
    assert.throws(() => reportInventory(options),
      /invalid retained retry identity at rows\[0\]\.retry_attempts\[0\]\.run_id/);
  }
});

test('CLI refuses cross-case retries before emitting manifest or native receipt matches', (t) => {
  const { root, saved, options } = nativeJournal(t);
  const identity = { run_id: 'original-run', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  Object.assign(saved.rows[0], identity);
  saved.rows[0].retry_attempts = [{ case_id: 'case-b', backend_run_id: 'native-provider' }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity], 2);
  const inputs = [options.report, options.journal, manifest];
  const before = inputs.map((file) => fs.readFileSync(file));
  const result = execute(root, process.execPath, [script, '--report', options.report,
    '--journal', options.journal, '--manifest', manifest, '--observed-at', String(now),
    '--evidence-class', options.evidenceClass, '--evidence-reference', options.evidenceReference]);
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '', 'no partial report may lend the root identity to this retry');
  assert.match(result.stderr, /invalid retained retry identity at rows\[0\]\.retry_attempts\[0\]\.case_id/);
  assert.deepEqual(inputs.map((file) => fs.readFileSync(file)), before);
});

test('report recovery requires explicit attribution and a valid check time', (t) => {
  const { options } = retainedReport(t);
  for (const overrides of [{ observedAt: NaN }, { observedAt: -1 },
    { evidenceClass: 'authenticated' }, { evidenceReference: '' }]) {
    assert.throws(() => reportInventory({ ...options, ...overrides }));
  }
});

test('report recovery aborts if the input changes during observation', (t) => {
  const { options } = retainedReport(t);
  const read = fs.readFileSync;
  let observations = 0;
  fs.readFileSync = function observed(file, ...args) {
    if (file === options.report && ++observations === 2) {
      fs.writeFileSync(file, '{"rows":[]}');
    }
    return read(file, ...args);
  };
  try {
    assert.throws(() => reportInventory(options), /changed during inventory/);
  } finally {
    fs.readFileSync = read;
  }
});

test('retained report reconciliation preserves requested order and explains every identity gap', (t) => {
  const { root, saved, options } = retainedReport(t);
  const identity = (run_id, verifier_verdict = 'NON_PASS', adjudicated_verdict = 'PASS') => ({
    run_id, target: 'owner/repo#1', verifier_verdict, adjudicated_verdict,
  });
  const requested = ['missing', 'wrong-target', 'wrong-verdict', 'incomplete',
    'duplicate', 'decided', 'shared'].map((run) => identity(run));
  saved.rows = [
    { ...identity('decided'), decision: 'reject_blocker', later_truth: 'PASS',
      backend_run_id: 'paid-before', raw_output: 'private payload',
      retry_attempts: [{ backend_run_id: 'paid-earlier' }] },
    { ...identity('wrong-target'), target: 'owner/repo#2' },
    identity('wrong-verdict', 'PASS', 'FAIL'),
    { ...identity('incomplete'), adjudicated_verdict: null },
    identity('duplicate'), identity('duplicate'),
    identity('shared', 'PASS', 'FAIL'), identity('shared'),
    identity('unrequested'),
  ].map((row, index) => ({ ...row, case_id: `retained-${index}` }));
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, requested, 2);
  const before = [options.report, manifest].map((file) => fs.readFileSync(file));
  const result = reportInventory({ ...options, manifest });
  const reconciliation = result.manifest_reconciliation;
  assert.equal(reconciliation.identity_scope, 'dispute');
  assert.equal(reconciliation.manifest_sha256, createHash('sha256').update(before[1]).digest('hex'));
  assert.equal(reconciliation.requested_count, requested.length);
  assert.deepEqual(reconciliation.cases.map((row) => row.run_id), requested.map((row) => row.run_id));
  assert.deepEqual(reconciliation.cases.map(({ status, reasons, retained_locations }) =>
    ({ status, reasons, retained_locations })), [
    { status: 'missing', reasons: ['requested_run_missing_from_report'], retained_locations: [] },
    { status: 'identity_mismatch', reasons: ['requested_target_mismatch'], retained_locations: ['rows[1]'] },
    { status: 'identity_mismatch', reasons: ['requested_verdict_mismatch'], retained_locations: ['rows[2]'] },
    { status: 'unavailable', reasons: ['retained_verdict_identity_incomplete'], retained_locations: ['rows[3]'] },
    { status: 'ambiguous', reasons: ['multiple_retained_rows_match_requested_identity'],
      retained_locations: ['rows[4]', 'rows[5]'] },
    { status: 'matched', reasons: [], retained_locations: ['rows[0]'] },
    { status: 'matched', reasons: [], retained_locations: ['rows[7]'] },
  ]);
  assert.equal(reconciliation.all_requested_rows_uniquely_matched, false);
  assert.deepEqual(reconciliation.cases.map((row) => row.retained_attempt_locations), [
    [], [], [], [], [], ['rows[0]', 'rows[0].retry_attempts[0]'], ['rows[7]'],
  ], 'only unique requested identities receive retained attempt evidence');
  assert.deepEqual(reconciliation.unmatched_retained_locations,
    ['rows[1]', 'rows[2]', 'rows[3]', 'rows[6]', 'rows[8]']);
  assert.equal(result.cases[0].retained_decision, 'reject_blocker');
  assert.equal(result.cases[0].retained_later_truth, 'PASS');
  assert.deepEqual(result.paid_attempts.map((row) => row.backend_run_id),
    ['paid-before', 'paid-earlier']);
  assert.equal(reconciliation.original_cohort_provenance_verified, false);
  assert.equal(reconciliation.authenticated_outcomes_verified, false);
  assert.equal(result.requested_cohort_verified, false);
  assert.equal(result.dispatch_authorized, false);
  assert.ok(!JSON.stringify(result).includes('private payload'));
  assert.deepEqual([options.report, manifest].map((file) => fs.readFileSync(file)), before);
});

test('retained report CLI matches supplied manifests without authenticating saved outcomes', (t) => {
  const { root, saved, options } = retainedReport(t);
  saved.rows.forEach((row, index) => Object.assign(row, {
    run_id: `run-${index}`, verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS',
  }));
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const requested = saved.rows.map(({ run_id, target, verifier_verdict, adjudicated_verdict }) =>
    ({ run_id, target, verifier_verdict, adjudicated_verdict })).reverse();
  const manifest = manifestFile(root, requested, 2);
  const files = fs.readdirSync(root);
  const result = execute(root, process.execPath, [script, '--report', options.report,
    '--observed-at', String(now), '--evidence-class', options.evidenceClass,
    '--evidence-reference', options.evidenceReference, '--manifest', manifest]);
  assert.equal(result.status, 0, result.stderr);
  const observed = JSON.parse(result.stdout);
  assert.deepEqual(observed, reportInventory({ ...options, manifest }));
  assert.equal(observed.manifest_reconciliation.all_requested_rows_uniquely_matched, true);
  assert.equal(observed.manifest_reconciliation.authenticated_outcomes_verified, false);
  assert.deepEqual(observed.manifest_reconciliation.unmatched_retained_locations, []);
  assert.equal(observed.receipt_authority, 'not_authenticated_by_inventory');
  assert.deepEqual(fs.readdirSync(root), [...files, 'stderr', 'stdout'].sort(),
    'only the test harness output captures may be created');
});

test('requested recovery dispositions explain identity gaps without borrowing candidate outcomes', (t) => {
  const { root, saved, options } = retainedReport(t);
  const identity = (run_id) => ({ run_id, target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' });
  saved.rows = [
    { ...identity('ambiguous'), case_id: 'a', error: 'packet requires gate runs' },
    { ...identity('ambiguous'), case_id: 'b', decision: 'reject_blocker' },
    { ...identity('changed'), case_id: 'c', target: 'owner/repo#2' },
  ];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, ['missing', 'ambiguous', 'changed'].map(identity), 2);
  const result = reportInventory({ ...options, manifest });
  for (const row of result.manifest_reconciliation.cases) {
    assert.equal(row.grading_evidence_gaps, null, 'unbound candidates cannot establish grading gaps');
    assert.deepEqual(row.recovery_disposition, {
      status: 'blocked', basis: 'retained_identity_gap', stage: 'identity_recovery',
      codes: row.reasons, evidence_locations: row.retained_locations,
      next_evidence_step: 'recover_unique_requested_row_before_using_attempt_evidence',
    });
  }
});

test('requested recovery keeps recorded failures separate from missing packet and truth evidence', (t) => {
  const { root, saved, options } = retainedReport(t);
  const identity = { run_id: 'original', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  const scenarios = [
    [{ error: 'backend exit=1 private diagnostic', errors: [
      'could not parse a JSON adjudication proposal from the backend output'] },
    'recorded_failure', 'backend_execution', 'backend_nonzero_exit'],
    [{ error: 'private unexplained failure' },
      'unresolved_retained_failure', null, 'retained_failure_cause_unresolved'],
    [{ decision: 'reject_blocker', error: 'packet requires gate runs' },
      'unresolved_retained_failure', null, 'retained_failure_cause_unresolved'],
    [{}, 'observed_evidence_gap', 'packet_evidence', 'packet_missing_or_invalid'],
    [{ packet: completePacket() },
      'observed_evidence_gap', 'attempt_completion', 'no_retained_decision'],
    [{ decision: 'private unrecognized decision' },
      'observed_evidence_gap', 'attempt_completion', 'unrecognized_retained_decision'],
    [{ packet: completePacket(), decision: 'needs_more_evidence' },
      'observed_evidence_gap', 'material_evidence', 'retained_abstention'],
    [{ packet: completePacket(), decision: 'reject_blocker', later_truth: 'PASS' },
      'observed_evidence_gap', 'native_receipt', 'backend_identity_not_recorded'],
  ];
  const manifest = manifestFile(root, [identity], 2);
  for (const [fields, basis, stage, code] of scenarios) {
    saved.rows = [{ ...identity, case_id: 'case-a', ...fields }];
    fs.writeFileSync(options.report, JSON.stringify(saved));
    const result = reportInventory({ ...options, manifest });
    const disposition = result.manifest_reconciliation.cases[0].recovery_disposition;
    assert.equal(disposition.status, 'blocked');
    assert.equal(disposition.basis, basis);
    assert.equal(disposition.stage, stage);
    assert.ok(disposition.codes.includes(code));
    assert.ok(disposition.evidence_locations.length > 0);
    assert.ok(disposition.next_evidence_step);
    assert.ok(result.manifest_reconciliation.cases[0].grading_evidence_gaps.some((gap) =>
      gap.stage === 'independent_later_truth'));
    assert.equal(result.manifest_reconciliation.authenticated_outcomes_verified, false);
    assert.equal(result.dispatch_authorized, false);
    assert.ok(!JSON.stringify({ disposition,
      gaps: result.manifest_reconciliation.cases[0].grading_evidence_gaps }).includes('private'));
  }
});

test('decided requested rows retain their outcome and UNKNOWN while historical retry failures stay separate', (t) => {
  const { root, saved, options } = nativeJournal(t);
  const identity = { run_id: 'original', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  saved.rows = [{ ...identity, case_id: 'case-a', packet: completePacket(),
    decision: 'reject_blocker', later_truth: 'UNKNOWN', backend_run_id: 'native-provider',
    retry_error: 'packet requires gate runs', retry_attempts: [{
      backend_run_id: 'primary', error: 'backend exit=1', later_truth: 'PASS' }] }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity], 2);
  const inputs = [options.report, options.journal, manifest];
  const before = inputs.map((file) => fs.readFileSync(file));
  const result = execute(root, process.execPath, [script, '--report', options.report,
    '--journal', options.journal, '--manifest', manifest, '--observed-at', String(now),
    '--evidence-class', options.evidenceClass, '--evidence-reference', options.evidenceReference]);
  assert.equal(result.status, 0, result.stderr);
  const observed = JSON.parse(result.stdout);
  const requested = observed.manifest_reconciliation.cases[0];
  assert.deepEqual(requested.recovery_disposition, {
    status: 'blocked', basis: 'observed_evidence_gap', stage: 'native_receipt',
    codes: ['matched_locator'], evidence_locations: ['rows[0]'],
    next_evidence_step: 'reauthenticate_with_native_projection_before_grading',
  });
  assert.deepEqual(requested.grading_evidence_gaps.map((item) => item.stage),
    ['native_receipt', 'independent_later_truth']);
  assert.equal(requested.grading_evidence_gaps[1].code, 'unknown');
  assert.equal(requested.grading_evidence_gaps[1].next_evidence_step,
    'preserve_UNKNOWN_until_independent_truth_available');
  assert.equal(observed.cases[0].retained_decision, 'reject_blocker');
  assert.equal(observed.cases[0].retained_later_truth, 'UNKNOWN');
  assert.equal(observed.retained_attempt_observations[1].first_broken_step.stage, 'backend_execution');
  assert.equal(observed.paid_attempts.length, 2);
  assert.equal(observed.manifest_reconciliation.authenticated_outcomes_verified, false);
  assert.deepEqual(inputs.map((file) => fs.readFileSync(file)), before);
});

test('possible paid attempts require completion recovery before packet recollection', (t) => {
  const { root, saved, options } = nativeJournal(t);
  const identity = { run_id: 'original', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  const manifest = manifestFile(root, [identity], 2);
  for (const backend of ['primary', 'started-provider', 'missing-provider']) {
    saved.rows = [{ ...identity, case_id: 'case-a', backend_run_id: backend }];
    fs.writeFileSync(options.report, JSON.stringify(saved));
    const result = reportInventory({ ...options, manifest });
    const recovery = result.manifest_reconciliation.cases[0].recovery_disposition;
    assert.equal(recovery.stage, 'native_receipt');
    assert.equal(recovery.next_evidence_step, {
      primary: 'resume_native_validation_without_redispatch',
      'started-provider': 'UNKNOWN_recover_provider_receipt_before_any_retry',
      'missing-provider': 'recover_native_receipt_without_redispatch',
    }[backend]);
    assert.equal(result.dispatch_authorized, false);
  }
});

test('requested rows expose unresolved native case attempts even without backend identities', (t) => {
  const { root, saved, options } = nativeJournal(t);
  const identity = { run_id: 'original', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  saved.rows = [{ ...identity, case_id: 'case-a', decision: 'reject_blocker',
    later_truth: 'UNKNOWN', retry_attempts: [{ backend_run_id: 'primary' }] }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity], 2);
  const inputs = [options.report, options.journal, manifest];
  const before = inputs.map((file) => fs.readFileSync(file));
  const result = reportInventory({ ...options, manifest });
  const recovery = result.manifest_reconciliation.cases[0].native_case_recovery;
  assert.equal(recovery.status, 'observed');
  assert.equal(recovery.scope, 'case_only_not_retained_attempt_binding');
  assert.deepEqual(recovery.unresolved_attempts.map(({ attempt_id, state,
    backend_run_id, next_evidence_step, location }) =>
    [attempt_id, state, backend_run_id, next_evidence_step, location]), [
    ['a', 'reserved', null, 'producer_must_prove_invocation_never_started',
      'pre_dispatch.native_journal.attempts[0]'],
    ['b', 'in_flight', null, 'UNKNOWN_recover_provider_receipt_before_any_retry',
      'pre_dispatch.native_journal.attempts[1]'],
    ['c', 'in_flight', 'started-provider', 'UNKNOWN_recover_provider_receipt_before_any_retry',
      'pre_dispatch.native_journal.attempts[2]'],
    ['d', 'raw_persisted', 'primary', 'resume_native_validation_without_redispatch',
      'pre_dispatch.native_journal.attempts[3]'],
    ['f', 'unrecognized', null, 'unrecognized_state_requires_native_review',
      'pre_dispatch.native_journal.attempts[5]'],
  ]);
  assert.equal(recovery.next_evidence_step, 'recover_native_case_attempts_before_any_fresh_dispatch');
  assert.equal(result.retained_attempt_observations[0].native_receipt_locator.status,
    'backend_identity_not_recorded', 'case evidence must not impersonate a bound receipt');
  assert.equal(result.cases[0].retained_decision, 'reject_blocker');
  assert.equal(result.cases[0].retained_later_truth, 'UNKNOWN');
  assert.equal(result.paid_attempts.length, 1, 'journal locators do not invent report paid identities');
  assert.equal(result.dispatch_authorized, false);
  assert.equal(result.manifest_reconciliation.authenticated_outcomes_verified, false);
  assert.ok(!JSON.stringify(result).includes('private'));
  const cli = execute(root, process.execPath, [script, '--report', options.report,
    '--journal', options.journal, '--manifest', manifest, '--observed-at', String(now),
    '--evidence-class', options.evidenceClass, '--evidence-reference', options.evidenceReference]);
  assert.equal(cli.status, 0, cli.stderr);
  assert.deepEqual(JSON.parse(cli.stdout), result);
  assert.deepEqual(inputs.map((file) => fs.readFileSync(file)), before);
});

test('native case recovery separates unavailable journals from no observed unresolved attempts', (t) => {
  const { root, saved, options } = nativeJournal(t);
  const identity = { run_id: 'original', target: 'owner/repo#1' };
  saved.rows = [{ ...identity, case_id: 'case-a' }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity]);
  const observe = (journal) => reportInventory({ ...options, journal, manifest })
    .manifest_reconciliation.cases[0].native_case_recovery;
  for (const journal of [undefined, path.join(root, 'missing.sqlite3')]) {
    const unavailable = observe(journal);
    assert.equal(unavailable.status, 'journal_unavailable');
    assert.equal(unavailable.unresolved_attempts, null);
    assert.equal(unavailable.next_evidence_step, 'recover_native_journal_without_redispatch');
  }
  assert.equal(fs.existsSync(path.join(root, 'missing.sqlite3')), false);
  const connection = new DatabaseSync(options.journal);
  connection.exec("DELETE FROM native_attempts WHERE state <> 'validated'");
  connection.close();
  assert.deepEqual(observe(options.journal), {
    scope: 'case_only_not_retained_attempt_binding', status: 'observed',
    unresolved_attempts: [], next_evidence_step: null,
  });
  const writer = new DatabaseSync(options.journal);
  writer.exec("UPDATE native_attempts SET state = 'in_flight'");
  writer.close();
  saved.rows[0].case_id = 'case-b';
  fs.writeFileSync(options.report, JSON.stringify(saved));
  assert.deepEqual(observe(options.journal).unresolved_attempts, [],
    'another case cannot lend its journal attempts');
});

test('unbound requested identities cannot borrow native case recovery from candidate rows', (t) => {
  const { root, saved, options } = nativeJournal(t);
  const identity = (run_id) => ({ run_id, target: 'owner/repo#1' });
  saved.rows = [{ ...identity('ambiguous'), case_id: 'case-a' },
    { ...identity('ambiguous'), case_id: 'case-b' },
    { ...identity('changed'), case_id: 'case-c', target: 'owner/repo#2' }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, ['ambiguous', 'changed', 'missing'].map(identity));
  const result = reportInventory({ ...options, manifest });
  for (const row of result.manifest_reconciliation.cases) {
    assert.equal(row.native_case_recovery, null);
  }
});

test('validated native failures and abstentions remain visible with their first recorded break', (t) => {
  const { root, saved, journal, options } = nativeJournal(t);
  const identity = { run_id: 'original', target: 'owner/repo#1' };
  saved.rows = [{ ...identity, case_id: 'case-a', decision: 'reject_blocker',
    later_truth: 'UNKNOWN', backend_run_id: 'native-provider' }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity]);
  const scenarios = [
    [{ errors: ['backend exit=1 private diagnostic',
      'could not parse a JSON adjudication proposal from the backend output'] },
    'retained_run_failure', 'recorded', 'backend_execution'],
    [{ proposal: { decision: 'needs_more_evidence' }, errors: [] },
      'retained_abstention', 'not_recorded', null],
    [{ proposal: { decision: 'reject_blocker' }, errors: ['private failure'] },
      'conflicting_retained_decision_and_failure', 'unresolved', null],
    [{ proposal: { decision: 'private unknown decision' }, errors: [] },
      'unrecognized_retained_decision', 'not_recorded', null],
    [{ proposal: null, errors: [] }, 'no_retained_decision', 'not_recorded', null],
  ];
  for (const [validation, status, breakStatus, stage] of scenarios) {
    const text = JSON.stringify({ ...validation, raw_output: 'private raw response' });
    const connection = new DatabaseSync(journal);
    connection.prepare("UPDATE native_attempts SET validated_json=? WHERE attempt_id='e'").run(text);
    connection.close();
    const inputs = [options.report, journal, manifest];
    const before = inputs.map((file) => fs.readFileSync(file));
    const result = reportInventory({ ...options, manifest });
    const attempt = result.pre_dispatch.native_journal.attempts.find((row) => row.attempt_id === 'e');
    const observed = attempt.validation_observation;
    assert.equal(observed.status, 'observed');
    assert.equal(observed.retained_result, status);
    assert.equal(observed.stored_text_sha256, createHash('sha256').update(text).digest('hex'));
    assert.equal(observed.first_broken_step.status, breakStatus);
    assert.equal(observed.first_broken_step.stage, stage);
    if (breakStatus === 'recorded') {
      assert.deepEqual(observed.first_broken_step.evidence_locations,
        ['pre_dispatch.native_journal.attempts[4].validated_json.errors[0]']);
    }
    const requested = result.manifest_reconciliation.cases[0];
    const unresolved = requested.native_case_recovery.unresolved_attempts;
    assert.deepEqual(unresolved.find((row) => row.attempt_id === 'e').validation_observation, observed);
    assert.deepEqual(result.retained_attempt_observations[0].native_receipt_locator
      .attempts[0].validation_observation, observed);
    assert.ok(result.pre_dispatch.stop_reasons.includes('native_validation_results_require_recovery'));
    assert.equal(result.cases[0].retained_decision, 'reject_blocker');
    assert.equal(result.cases[0].retained_later_truth, 'UNKNOWN');
    assert.equal(result.dispatch_authorized, false);
    assert.equal(result.manifest_reconciliation.authenticated_outcomes_verified, false);
    assert.ok(!JSON.stringify(result).includes('private'));
    const cli = execute(root, process.execPath, [script, '--report', options.report,
      '--journal', journal, '--manifest', manifest, '--observed-at', String(now),
      '--evidence-class', options.evidenceClass, '--evidence-reference', options.evidenceReference]);
    assert.equal(cli.status, 0, cli.stderr);
    assert.deepEqual(JSON.parse(cli.stdout), result);
    assert.deepEqual(inputs.map((file) => fs.readFileSync(file)), before);
  }
});

test('malformed or missing native validation evidence cannot disappear behind validated state', (t) => {
  const { root, saved, journal, options } = nativeJournal(t);
  const identity = { run_id: 'original', target: 'owner/repo#1' };
  saved.rows = [{ ...identity, case_id: 'case-a' }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity]);
  for (const [value, status] of [[null, 'not_recorded'], ['private invalid json', 'invalid_json'],
    ['null', 'invalid_shape'], ['[]', 'invalid_shape'], ['"private"', 'invalid_shape']]) {
    const connection = new DatabaseSync(journal);
    connection.prepare("UPDATE native_attempts SET validated_json=? WHERE attempt_id='e'").run(value);
    connection.close();
    const result = reportInventory({ ...options, manifest });
    const attempt = result.manifest_reconciliation.cases[0].native_case_recovery
      .unresolved_attempts.find((row) => row.attempt_id === 'e');
    assert.ok(attempt, 'validated state must not hide missing or unreadable results');
    assert.equal(attempt.validation_observation.status, status);
    assert.equal(attempt.next_evidence_step, 'recover_missing_native_receipt_evidence_without_redispatch');
    assert.ok(result.pre_dispatch.stop_reasons.includes('native_receipt_evidence_missing'));
    assert.ok(!JSON.stringify(result).includes('private'));
    assert.equal(result.dispatch_authorized, false);
  }
});

test('binary validation still requires receipt presence and remains unauthenticated', (t) => {
  const { root, saved, journal, options } = nativeJournal(t);
  const identity = { run_id: 'original', target: 'owner/repo#1' };
  saved.rows = [{ ...identity, case_id: 'case-a', backend_run_id: 'native-provider' }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity]);
  for (const [column, value, code] of [['raw_response', null, 'raw_response_missing'],
    ['backend_run_id', null, 'backend_identity_missing'],
    ['backend_run_id', ' ', 'backend_identity_missing']]) {
    const connection = new DatabaseSync(journal);
    connection.prepare(`UPDATE native_attempts SET ${column}=? WHERE attempt_id='e'`).run(value);
    connection.close();
    const result = reportInventory({ ...options, manifest });
    const unresolved = result.manifest_reconciliation.cases[0].native_case_recovery.unresolved_attempts;
    const attempt = unresolved.find((row) => row.attempt_id === 'e');
    assert.ok(attempt, 'a saved binary proposal cannot replace missing receipt fields');
    assert.equal(attempt.validation_observation.retained_result, 'retained_binary_decision');
    assert.ok(attempt.receipt_evidence_gaps.includes(code));
    assert.equal(attempt.next_evidence_step, 'recover_missing_native_receipt_evidence_without_redispatch');
  }
  const connection = new DatabaseSync(journal);
  connection.prepare("UPDATE native_attempts SET raw_response=?,backend_run_id=? WHERE attempt_id='e'")
    .run('private raw response', 'native-provider');
  connection.close();
  const result = reportInventory({ ...options, manifest });
  assert.ok(!result.manifest_reconciliation.cases[0].native_case_recovery.unresolved_attempts
    .some((row) => row.attempt_id === 'e'));
  const locator = result.retained_attempt_observations[0].native_receipt_locator;
  assert.equal(locator.authenticated, false);
  assert.equal(locator.next_evidence_step, 'reauthenticate_with_native_projection_before_grading');
  assert.equal(result.dispatch_authorized, false);
});

test('native binary results with inconsistent receipt fields remain unresolved', (t) => {
  const { root, saved, journal, options } = nativeJournal(t);
  const identity = { run_id: 'original', target: 'owner/repo#1' };
  saved.rows = [{ ...identity, case_id: 'case-a', backend_run_id: 'native-provider',
    decision: 'reject_blocker', later_truth: 'UNKNOWN' }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity]);
  const scenarios = [
    ['backend_run_id', undefined, 'validation_backend_identity_missing'],
    ['backend_run_id', null, 'validation_backend_identity_missing'],
    ['backend_run_id', 42, 'validation_backend_identity_missing'],
    ['backend_run_id', ' ', 'validation_backend_identity_missing'],
    ['backend_run_id', 'private different backend', 'validation_backend_identity_mismatch'],
    ['backend_run_id', 'native-provider ', 'validation_backend_identity_mismatch'],
    ['raw_output', undefined, 'validation_raw_output_missing_or_invalid'],
    ['raw_output', null, 'validation_raw_output_missing_or_invalid'],
    ['raw_output', { text: 'private raw response' }, 'validation_raw_output_missing_or_invalid'],
    ['raw_output', '', 'validation_raw_output_mismatch'],
    ['raw_output', 'private raw response\n', 'validation_raw_output_mismatch'],
    ['raw_output', 'private different response', 'validation_raw_output_mismatch'],
  ];
  for (const [field, value, code] of scenarios) {
    const validation = { proposal: { decision: 'reject_blocker' }, errors: [],
      backend_run_id: 'native-provider', raw_output: 'private raw response', [field]: value };
    const connection = new DatabaseSync(journal);
    connection.prepare("UPDATE native_attempts SET validated_json=? WHERE attempt_id='e'")
      .run(JSON.stringify(validation));
    connection.close();
    const inputs = [options.report, journal, manifest];
    const before = inputs.map((file) => fs.readFileSync(file));
    const result = reportInventory({ ...options, manifest });
    const attempt = result.pre_dispatch.native_journal.attempts.find((row) => row.attempt_id === 'e');
    assert.deepEqual(attempt.receipt_evidence_gaps, [code], `${field}: ${value}`);
    assert.equal(attempt.validation_observation.retained_result, 'retained_binary_decision');
    assert.equal(attempt.recovery_step, 'recover_missing_native_receipt_evidence_without_redispatch');
    assert.deepEqual(result.manifest_reconciliation.cases[0].native_case_recovery
      .unresolved_attempts.find((row) => row.attempt_id === 'e').receipt_evidence_gaps, [code]);
    const locator = result.retained_attempt_observations[0].native_receipt_locator;
    assert.deepEqual(locator.attempts[0].receipt_evidence_gaps, [code]);
    assert.equal(locator.authenticated, false);
    assert.ok(result.pre_dispatch.stop_reasons.includes('native_receipt_evidence_missing'));
    assert.equal(result.cases[0].retained_decision, 'reject_blocker');
    assert.equal(result.cases[0].retained_later_truth, 'UNKNOWN');
    assert.equal(result.dispatch_authorized, false);
    assert.equal(result.manifest_reconciliation.authenticated_outcomes_verified, false);
    assert.ok(!JSON.stringify(result).includes('private'));
    assert.deepEqual(inputs.map((file) => fs.readFileSync(file)), before);
  }
});

test('native receipt consistency compares exact stored text without parsing provider output', (t) => {
  const { root, saved, journal, options } = nativeJournal(t);
  const identity = { run_id: 'original', target: 'owner/repo#1' };
  saved.rows = [{ ...identity, case_id: 'case-a' }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity]);
  for (const text of ['', 'private non-JSON response', 'private café\r\n', 'private cafe\u0301\n']) {
    const connection = new DatabaseSync(journal);
    const result = JSON.stringify({ proposal: { decision: 'reject_blocker' }, errors: [],
      backend_run_id: 'native-provider', raw_output: text });
    connection.prepare("UPDATE native_attempts SET raw_response=?,validated_json=? WHERE attempt_id='e'")
      .run(text, result);
    connection.close();
    const observed = reportInventory({ ...options, manifest });
    const attempt = observed.pre_dispatch.native_journal.attempts.find((row) => row.attempt_id === 'e');
    assert.deepEqual(attempt.validation_observation.receipt_binding_gaps, []);
    assert.deepEqual(attempt.receipt_evidence_gaps, []);
    assert.ok(!observed.manifest_reconciliation.cases[0].native_case_recovery.unresolved_attempts
      .some((row) => row.attempt_id === 'e'));
    assert.equal(attempt.recovery_step, 'reauthenticate_with_native_projection_before_grading');
    assert.equal(observed.receipt_authority, 'not_authenticated_by_inventory');
    assert.equal(observed.dispatch_authorized, false);
    assert.ok(!JSON.stringify(observed).includes('private'));
  }
});

test('native receipt consistency CLI retains both mismatches without leaking or changing inputs', (t) => {
  const { root, saved, journal, options } = nativeJournal(t);
  const identity = { run_id: 'original', target: 'owner/repo#1' };
  saved.rows = [{ ...identity, case_id: 'case-a', backend_run_id: 'native-provider',
    retry_attempts: [{ backend_run_id: 'primary' }] }];
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [identity]);
  const connection = new DatabaseSync(journal);
  connection.prepare("UPDATE native_attempts SET validated_json=? WHERE attempt_id='e'")
    .run(JSON.stringify({ proposal: { decision: 'reject_blocker' }, errors: [],
      backend_run_id: 'private conflicting id', raw_output: 'private conflicting output' }));
  connection.close();
  const inputs = [options.report, journal, manifest];
  const before = inputs.map((file) => fs.readFileSync(file));
  const result = execute(root, process.execPath, [script, '--report', options.report,
    '--journal', journal, '--manifest', manifest, '--observed-at', String(now),
    '--evidence-class', options.evidenceClass, '--evidence-reference', options.evidenceReference]);
  assert.equal(result.status, 0, result.stderr);
  const observed = JSON.parse(result.stdout);
  assert.deepEqual(observed, reportInventory({ ...options, manifest }));
  const locator = observed.retained_attempt_observations[0].native_receipt_locator;
  assert.deepEqual(locator.attempts[0].receipt_evidence_gaps,
    ['validation_backend_identity_mismatch', 'validation_raw_output_mismatch']);
  assert.equal(locator.next_evidence_step, 'recover_missing_native_receipt_evidence_without_redispatch');
  assert.equal(observed.retained_attempt_observations[1].native_receipt_locator.next_evidence_step,
    'resume_native_validation_without_redispatch', 'a mismatched result cannot taint another attempt');
  assert.equal(observed.paid_identity_summary.unique, 2);
  assert.equal(observed.manifest_reconciliation.authenticated_outcomes_verified, false);
  assert.equal(observed.dispatch_authorized, false);
  assert.ok(!result.stdout.includes('private'));
  assert.deepEqual(inputs.map((file) => fs.readFileSync(file)), before);
});

test('legacy report manifest keeps verdict ambiguity visible and empty requests cannot complete accounting', (t) => {
  const { root, saved, options } = retainedReport(t);
  saved.rows = ['NON_PASS', 'FAIL'].map((verifier_verdict, index) => ({
    case_id: `case-${index}`, run_id: 'same', target: 'owner/repo#1', verifier_verdict,
    adjudicated_verdict: 'PASS',
  }));
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const manifest = manifestFile(root, [{ run_id: 'same', target: 'owner/repo#1' }]);
  const legacy = reportInventory({ ...options, manifest }).manifest_reconciliation;
  assert.equal(legacy.identity_scope, 'run_target');
  assert.equal(legacy.cases[0].status, 'ambiguous');
  assert.deepEqual(legacy.cases[0].retained_locations, ['rows[0]', 'rows[1]']);
  manifestFile(root, []);
  const empty = reportInventory({ ...options, manifest }).manifest_reconciliation;
  assert.equal(empty.all_requested_rows_uniquely_matched, false);
  assert.deepEqual(empty.unmatched_retained_locations, ['rows[0]', 'rows[1]']);
});

test('report reconciliation refuses malformed or changing manifests without replacing evidence', (t) => {
  const { root, options } = retainedReport(t);
  const manifest = manifestFile(root, [{ run_id: 'original', target: 'owner/repo#1' }]);
  const read = fs.readFileSync;
  let observations = 0;
  fs.readFileSync = function observed(file, ...args) {
    if (file === manifest && ++observations === 2) fs.writeFileSync(file, '{"cases":[]}');
    return read(file, ...args);
  };
  try {
    assert.throws(() => reportInventory({ ...options, manifest }), /manifest changed during inventory/);
  } finally {
    fs.readFileSync = read;
  }
  assert.throws(() => reportInventory({ ...options, manifest }), /manifest requires/);
  assert.equal(reportInventory(options).manifest_reconciliation, null);
});

test('report CLI never combines historical recovery with dispatch or current Brain selection', (t) => {
  const { root, options } = retainedReport(t);
  const args = [script, '--report', options.report, '--observed-at', String(now),
    '--evidence-class', options.evidenceClass, '--evidence-reference', options.evidenceReference];
  const result = execute(root, process.execPath, args);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), reportInventory(options));
  for (const extra of [['--dispatch'], ['--db', 'absent.db'], ['--manifest', 'absent.json'],
    ['--days', '90'], ['--expected-count', '58']]) {
    const refused = execute(root, process.execPath, [...args, ...extra]);
    assert.equal(refused.status, 1, refused.stderr);
    assert.equal(refused.stdout, '', 'refusal must not publish a partial inventory');
  }
});

function nativeJournal(t) {
  const fixture = retainedReport(t);
  const journal = path.join(fixture.root, 'native.sqlite3');
  const connection = new DatabaseSync(journal);
  connection.exec(`PRAGMA application_id=1313097550;
    CREATE TABLE native_attempts (attempt_id TEXT PRIMARY KEY, case_id TEXT,
      material_revision TEXT, case_digest TEXT, state TEXT, backend_run_id TEXT,
      report_sha256 TEXT, created_at INTEGER, updated_at INTEGER,
      raw_response TEXT, validated_json TEXT);`);
  const insert = connection.prepare('INSERT INTO native_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)');
  for (const [id, state, backend, raw, validation] of [
    ['a', 'reserved', null, null, null],
    ['b', 'in_flight', null, null, null],
    ['c', 'in_flight', 'started-provider', null, null],
    ['d', 'raw_persisted', 'primary', 'private raw response', null],
    ['e', 'validated', 'native-provider', 'private raw response',
      JSON.stringify({ proposal: { decision: 'reject_blocker', rationale: 'private proposal' },
        backend_run_id: 'native-provider', raw_output: 'private raw response', errors: [] })],
    ['f', 'unrecognized', null, null, null],
  ]) insert.run(id, 'case-a', `revision-${id}`, 'digest', state, backend, 'source-hash',
    now - 1, now, raw, validation);
  connection.close();
  return { ...fixture, journal, options: { ...fixture.options, journal } };
}

test('native receipt locators bind both case and backend identity for each retained attempt', (t) => {
  const { root, saved, journal, options } = nativeJournal(t);
  saved.rows[0].retry_attempts = [{ backend_run_id: 'native-provider' }];
  saved.rows[1].backend_run_id = 'native-provider';
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const before = [options.report, journal].map((file) => fs.readFileSync(file));
  const files = fs.readdirSync(root);
  const result = reportInventory(options);
  const [parent, retry, otherCase] = result.retained_attempt_observations;
  assert.equal(parent.native_receipt_locator.status, 'matched_locator');
  assert.equal(parent.native_receipt_locator.attempts[0].attempt_id, 'd');
  assert.equal(parent.native_receipt_locator.attempts[0].state, 'raw_persisted');
  assert.equal(parent.native_receipt_locator.next_evidence_step,
    'resume_native_validation_without_redispatch');
  assert.equal(retry.native_receipt_locator.status, 'matched_locator');
  assert.equal(retry.native_receipt_locator.attempts[0].attempt_id, 'e');
  assert.equal(retry.native_receipt_locator.next_evidence_step,
    'reauthenticate_with_native_projection_before_grading');
  assert.equal(otherCase.native_receipt_locator.status, 'case_identity_mismatch');
  assert.equal(otherCase.native_receipt_locator.attempts[0].case_id, 'case-a');
  for (const row of result.retained_attempt_observations) {
    assert.equal(row.native_receipt_locator.authenticated, false);
  }
  assert.equal(result.dispatch_authorized, false);
  assert.equal(Object.hasOwn(result, 'graded'), false);
  assert.ok(!JSON.stringify(result).includes('private'));
  assert.deepEqual([options.report, journal].map((file) => fs.readFileSync(file)), before);
  assert.deepEqual(fs.readdirSync(root), files);
});

test('missing native identities and receipts remain gaps rather than borrowing another attempt', (t) => {
  const { saved, options } = nativeJournal(t);
  delete saved.rows[0].backend_run_id;
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const observations = reportInventory(options).retained_attempt_observations;
  assert.equal(observations[0].native_receipt_locator.status, 'backend_identity_not_recorded');
  assert.deepEqual(observations[0].native_receipt_locator.attempts, []);
  assert.equal(observations[1].native_receipt_locator.status, 'native_attempt_not_found');
  assert.deepEqual(observations[1].native_receipt_locator.attempts, []);
  const unavailable = reportInventory({ ...options, journal: undefined })
    .retained_attempt_observations;
  assert.equal(unavailable[0].native_receipt_locator.status, 'backend_identity_not_recorded');
  assert.equal(unavailable[1].native_receipt_locator.status, 'journal_unavailable');
  assert.deepEqual(unavailable[1].native_receipt_locator.attempts, []);
  assert.equal(unavailable[1].native_receipt_locator.authenticated, false);
});

test('reused native backend identities remain ambiguous even when one candidate matches the case', (t) => {
  const { journal, options } = nativeJournal(t);
  const connection = new DatabaseSync(journal);
  connection.exec(`INSERT INTO native_attempts
    SELECT 'duplicate', 'case-b', material_revision, case_digest, state, backend_run_id,
      report_sha256, created_at, updated_at, raw_response, validated_json
    FROM native_attempts WHERE attempt_id='d'`);
  connection.close();
  const locator = reportInventory(options).retained_attempt_observations[0].native_receipt_locator;
  assert.equal(locator.status, 'ambiguous_backend_identity');
  assert.deepEqual(locator.attempts.map((row) => row.attempt_id), ['d', 'duplicate']);
  assert.equal(locator.next_evidence_step, 'resolve_native_receipt_identity_before_any_retry');
  assert.equal(locator.authenticated, false);
});

test('pre-dispatch inventory reconciles report and native locators without exposing payloads', (t) => {
  const { root, journal, options } = nativeJournal(t);
  const before = fs.readFileSync(journal);
  const files = fs.readdirSync(root);
  const result = reportInventory(options).pre_dispatch;
  assert.equal(result.generic_retry_route, 'refused_native_canonical_publication');
  assert.equal(result.native_journal.status, 'available');
  assert.equal(result.native_journal.receipt_authority, 'not_authenticated_by_inventory');
  assert.deepEqual(result.paid_identity_reconciliation, {
    report_unique: 3, journal_unique: 3, union_unique: 5,
    report_only: ['prior', 'oldest'], journal_only: ['started-provider', 'native-provider'],
    unresolved_without_backend_id: ['b'],
  });
  const attempts = result.native_journal.attempts;
  assert.deepEqual(attempts.map((item) => item.attempt_id), ['a', 'b', 'c', 'd', 'e', 'f']);
  assert.equal(attempts[0].recovery_step, 'producer_must_prove_invocation_never_started');
  assert.equal(attempts[1].recovery_step, 'UNKNOWN_recover_provider_receipt_before_any_retry');
  assert.equal(attempts[3].recovery_step, 'resume_native_validation_without_redispatch');
  assert.equal(attempts[4].recovery_step, 'reauthenticate_with_native_projection_before_grading');
  assert.equal(attempts[5].recovery_step, 'unrecognized_state_requires_native_review');
  assert.equal(attempts[4].raw_receipt_present, true);
  assert.equal(attempts[4].validation_present, true);
  assert.equal(attempts[1].raw_receipt_present, false);
  assert.ok(!JSON.stringify(result).includes('private'));
  assert.equal(result.live_writer_ownership, 'unverified');
  assert.equal(result.dispatch_authorized, false);
  assert.ok(result.stop_reasons.includes('unresolved_native_attempts_require_recovery'));
  assert.deepEqual(fs.readFileSync(journal), before);
  assert.deepEqual(fs.readdirSync(root), files, 'read-only observation creates no lock or runtime');
  assert.ok(!reportInventory(options).evidence_gaps.includes('native_journal_not_observed'));
  assert.ok(reportInventory(options).evidence_gaps.includes('native_journal_receipts_not_authenticated'));
});

test('all validated native rows still require authentication, independent truth and live writer proof', (t) => {
  const { journal, options } = nativeJournal(t);
  const connection = new DatabaseSync(journal);
  connection.exec("DELETE FROM native_attempts WHERE state != 'validated'");
  connection.close();
  const result = reportInventory(options).pre_dispatch;
  assert.equal(result.dispatch_authorized, false);
  assert.ok(!result.stop_reasons.includes('unresolved_native_attempts_require_recovery'));
  assert.ok(result.stop_reasons.includes('live_writer_ownership_unverified'));
  assert.ok(result.stop_reasons.includes('native_receipt_authentication_and_independent_truth_required'));
  assert.equal(Object.hasOwn(result, 'graded'), false);
});

test('missing native evidence remains unavailable and never means zero attempts', (t) => {
  const { root, options } = retainedReport(t);
  for (const journal of [undefined, path.join(root, 'absent', 'native.sqlite3')]) {
    const result = reportInventory({ ...options, journal }).pre_dispatch;
    assert.equal(result.native_journal.status, 'unavailable');
    assert.equal(result.native_journal.attempts, null);
    assert.equal(result.paid_identity_reconciliation.journal_unique, null);
    assert.equal(result.paid_identity_reconciliation.union_unique, null);
    assert.equal(result.dispatch_authorized, false);
  }
  assert.equal(fs.existsSync(path.join(root, 'absent')), false);
});

test('an observed empty journal cannot establish writer ownership or authorize spending', (t) => {
  const { journal, options } = nativeJournal(t);
  const connection = new DatabaseSync(journal);
  connection.exec('DELETE FROM native_attempts');
  connection.close();
  const result = reportInventory(options).pre_dispatch;
  assert.equal(result.native_journal.status, 'available');
  assert.deepEqual(result.native_journal.attempts, []);
  assert.equal(result.paid_identity_reconciliation.journal_unique, 0);
  assert.equal(result.paid_identity_reconciliation.union_unique, 3);
  assert.equal(result.live_writer_ownership, 'unverified');
  assert.equal(result.dispatch_authorized, false);
});

test('an active journal writer does not make a read-only snapshot claim ownership', (t) => {
  const { journal, options } = nativeJournal(t);
  const connection = new DatabaseSync(journal);
  connection.exec("BEGIN IMMEDIATE; DELETE FROM native_attempts");
  try {
    const result = reportInventory(options).pre_dispatch;
    assert.equal(result.native_journal.status, 'available');
    assert.equal(result.native_journal.attempts.length, 6, 'uncommitted changes are invisible');
    assert.equal(result.live_writer_ownership, 'unverified');
    assert.equal(result.dispatch_authorized, false);
  } finally {
    connection.exec('ROLLBACK');
    connection.close();
  }
});

test('foreign and incomplete journals are refused without schema repair', (t) => {
  const { root, options } = retainedReport(t);
  const journal = path.join(root, 'foreign.sqlite3');
  const connection = new DatabaseSync(journal);
  connection.exec('CREATE TABLE unrelated (id TEXT)');
  connection.close();
  for (const reason of ['non_native_journal', 'native_journal_unreadable_or_schema_unavailable']) {
    const before = fs.readFileSync(journal);
    const result = reportInventory({ ...options, journal }).pre_dispatch.native_journal;
    assert.equal(result.status, 'unavailable');
    assert.equal(result.reason, reason);
    assert.equal(result.attempts, null);
    assert.deepEqual(fs.readFileSync(journal), before);
    const db = new DatabaseSync(journal);
    db.exec('PRAGMA application_id=1313097550');
    db.close();
  }
});

test('report without native marker still cannot authorize generic retry', (t) => {
  const { saved, options } = retainedReport(t);
  delete saved.native_publication;
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const result = reportInventory(options).pre_dispatch;
  assert.equal(result.generic_retry_route, 'not_authorized_by_inventory');
  assert.equal(result.dispatch_authorized, false);
});

test('journal CLI stays read-only and requires explicit report attribution', (t) => {
  const { root, journal, options } = nativeJournal(t);
  const args = [script, '--report', options.report, '--journal', journal,
    '--observed-at', String(now), '--evidence-class', options.evidenceClass,
    '--evidence-reference', options.evidenceReference];
  const result = execute(root, process.execPath, args);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), reportInventory(options));
  const refused = execute(root, process.execPath, [script, '--db', 'absent.db',
    '--journal', journal, '--observed-at', String(now)]);
  assert.equal(refused.status, 1);
  assert.equal(refused.stdout, '');
  assert.match(refused.stderr, /require --report/);
});

for (const [variant, excludedTarget] of [
  ['mixed case', 'StRaNsKe/CoLlAb-AdMiN#1'],
  ['spaces', ' stranske/Collab-Admin #1 '],
  ['tabs and newlines', '\tStRaNsKe/CoLlAb-AdMiN\r\n#1'],
  ['Unicode whitespace', '\u00a0stranske/Collab-Admin\u2003#1'],
]) {
  test(`owner exclusions are accounted for without reading excluded outcome evidence: ${variant}`,
    (t) => checkOwnerExclusion(t, excludedTarget));
}

function checkOwnerExclusion(t, excludedTarget) {
  const { root, db } = world(t);
  const connection = new DatabaseSync(db);
  connection.prepare('INSERT INTO runs VALUES (?,?,?)').run('excluded', excludedTarget, now);
  connection.exec(`
    INSERT INTO runs VALUES
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
    run_id, target: run_id === 'excluded' ? excludedTarget :
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
}

for (const target of ['stranske/Collab-Admin#7', '\tstranske/Collab-Admin\u00a0#7']) {
  test(`requested owner exclusions remain explicit with missing or contradictory inventory: ${JSON.stringify(target)}`,
    (t) => checkRequestedOwnerExclusion(t, target));
}

function checkRequestedOwnerExclusion(t, target) {
  const { root, db } = world(t);
  seed(db, [['reused-run', now, 'NON_PASS', 'PASS']]);
  const requested = ['reused-run', 'absent'].map((run_id) => ({
    run_id, target,
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
}

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
