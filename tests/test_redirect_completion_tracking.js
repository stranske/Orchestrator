'use strict';

// Check the retained operational handoff, without mutating GitHub or runtime state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const evidenceDir = path.resolve(__dirname, '../docs/verification/issue-1106');
const receipt = (name) => JSON.parse(fs.readFileSync(path.join(evidenceDir, name), 'utf8'));
const proposal = fs.readFileSync(path.join(evidenceDir, 'proposed-pr-body.md'), 'utf8');
const checklist = proposal.split('\n').flatMap((line) => {
  const match = /^- \[([ xX])\] (.+)$/.exec(line);
  return match ? [{ checked: match[1].toLowerCase() === 'x', text: match[2] }] : [];
});

function expectTracking(prefix, checked) {
  const copies = checklist.filter((row) => row.text.startsWith(prefix));
  assert.equal(copies.length, 2, `summary and full issue must both track: ${prefix}`);
  for (const row of copies) assert.equal(row.checked, checked, row.text);
}

test('offline completion is backed by named executions, focused files and clean replay', () => {
  const after = receipt('merged-revision-after.json');
  const before = receipt('merged-revision-before.json');
  const interpreter = receipt('interpreter-preflight.json');
  assert.equal(after.status, 'verified');
  assert.equal(before.status, 'blocked', 'the original process-error run stays blocked');
  assert.equal(before.revision, after.revision);
  assert.equal(after.revision, interpreter.source_revision);
  assert.match(after.revision, /^[a-f0-9]{40}$/);
  assert.match(after.archive_sha256, /^[a-f0-9]{64}$/);
  assert.equal(after.operational_acceptance, 'not_established');
  assert.equal(after.historical_replay_receipt, 'not_established');
  assert.deepEqual(after.executions.map((row) => row.name),
    ['interpreter', 'named_tests', 'focused_files', 'offline_replay']);
  for (const row of after.executions) {
    assert.equal(row.exit_code, 0, row.name);
    assert.equal(row.signal, null, row.name);
    assert.equal(row.error_code, null, row.name);
    assert.equal(row.argv[0], interpreter.executable, row.name);
  }
  const identity = JSON.parse(after.executions[0].stdout);
  assert.equal(identity.executable, interpreter.executable);
  assert.equal(identity.version, interpreter.version);
  const named = after.executions[1];
  const focused = after.executions[2];
  const nodes = [
    'tests/test_redirect_chain_gate.py::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine',
    'tests/test_redirect_apply_prescreen.py::test_redirect_apply_daily_cap_refuses_second_apply',
  ];
  for (const node of nodes) {
    assert.ok(named.argv.includes(node));
    assert.ok(named.stdout.split('\n').some((line) => line.startsWith(`${node} PASSED `)));
    const file = node.split('::')[0];
    assert.ok(focused.argv.includes(file));
    assert.ok(focused.stdout.split('\n').some((line) => line.startsWith(`${file}::`)
      && line.includes(' PASSED ')));
  }
  for (const row of [named, focused]) {
    assert.deepEqual(row.argv.slice(-2), ['-m', 'not slow']);
    assert.match(row.stdout.trim().split('\n').at(-1), /\b\d+ passed\b/);
    assert.doesNotMatch(row.stdout.trim().split('\n').at(-1), /skipped|failed|error/i);
  }
  const replay = after.executions[3].stdout.trim().match(/^authorized (\d+)\/12 with named agents \(dry-run\)$/);
  assert.ok(replay);
  assert.equal(Number(replay[1]), after.authorized);
  assert.ok(after.authorized >= 9 && after.authorized <= 12);
  expectTracking('Run the merged exact-revision verifier', true);
  expectTracking('An identified merged revision has successful real named-test', true);
});

test('unavailable owner observations keep historical inspection and real-stall acceptance open', () => {
  const observations = [
    receipt('owner-path-observation.json'),
    receipt('owner-path-observation-followup.json'),
    receipt('collector-cli-followup.json').observation,
  ];
  for (const observation of observations) {
    assert.equal(observation.status, 'unavailable');
    for (const field of ['blocking', 'drainable', 'candidates']) {
      assert.equal(observation[field], null, `${field} is unknown, not zero`);
    }
    assert.equal(observation.operational_acceptance, 'not_established');
    assert.ok(Number.isFinite(Date.parse(observation.observed_at)));
  }
  const dates = observations.map((row) => Date.parse(row.observed_at));
  assert.ok(dates[0] < dates[1] && dates[1] < dates[2], 'later checks retain their own dates');
  expectTracking('Inspect retained November 3 owner-host artifacts', false);
  expectTracking('Observe the next eligible real stall', false);
  expectTracking('One eligible real stall has an attributable bounded apply', false);
  expectTracking('If November 3 evidence is unrecoverable', true);
  assert.match(proposal, /historical and real-stall acceptance remain unmet/);
});

test('pending remote reconciliation cannot introduce a new automatic closure directive', () => {
  assert.equal(receipt('collector-cli-recovery-search.json').reconciliation.complete, false);
  expectTracking('Reconcile completion tracking with the actual evidence', false);
  // Preserve the historical verifier quotation, but never emit an active closer.
  assert.doesNotMatch(proposal, /^(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:stranske\/Orchestrator)?#(?:1088|1106)\b/im);
  assert.match(proposal, /Related to #1106; operational acceptance remains incomplete\./);
});
