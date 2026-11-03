'use strict';

const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { propose } = require('../scripts/reconcile_adjudicator_completion');

const fixture = path.join(__dirname, 'fixtures/adjudicator-completion-before.json');
const snapshot = () => JSON.parse(fs.readFileSync(fixture, 'utf8'));
const successorSnapshot = () => ({
  ...snapshot(),
  successor: JSON.parse(fs.readFileSync(
    path.join(__dirname, 'fixtures/adjudicator-completion-successor-before.json'), 'utf8')),
});
const reportClaim = 'Reconcile the saved report and switch-review output with the actual run, and correct completion metadata so bounded work cannot imply broad acceptance.';
const mutationClaim = 'Enabling an outcomes write fails the second named test; enabling uncontested offers fails the third; exact restoration passes.';
const namedClaim = 'All three named acceptance tests pass on the evaluated source.';

test('corrected claims distinguish transferred work from acceptance without reopening the handoff', () => {
  const before = snapshot();
  const original = structuredClone(before);
  const result = propose(before);
  assert.equal(result.issue.expected_state, 'closed');
  assert.ok(!Object.hasOwn(result.issue, 'state'));
  assert.ok(!Object.hasOwn(result.pull_request, 'state'));
  assert.equal(result.operational_acceptance_complete, false);
  assert.equal(result.remote_metadata_verified, false);
  assert.deepEqual(before, original, 'the input snapshot is immutable');
  for (const key of ['issue', 'pull_request']) {
    assert.equal(result[key].expected_body_sha256, createHash('sha256')
      .update(before[key].body).digest('hex'));
    assert.match(result[key].body, /Operational acceptance remains incomplete/);
    assert.match(result[key].body, /6ed980924ec1d7d83a06f9b6a181dc76ba5eb910/);
    assert.match(result[key].body, /do not establish current-source/);
    assert.match(result[key].body, /Remaining operational work is tracked in #1091/);
    assert.match(result[key].body, /records that handoff, not successful operational acceptance/);
  }
});

test('both PR checklist copies leave current-source mutations and report reconciliation pending', () => {
  const before = snapshot();
  // Reproduce inflated checkboxes in both copies, even if only one was checked
  // in the captured PR. Historical tests cannot validate either copy.
  for (const claim of [reportClaim, mutationClaim, namedClaim]) {
    before.pull_request.body = before.pull_request.body.replaceAll(`- [ ] ${claim}`, `- [x] ${claim}`);
  }
  const after = propose(before).pull_request.body;
  for (const claim of [reportClaim, mutationClaim, namedClaim]) {
    assert.equal(after.split(`- [ ] ${claim}`).length - 1, 2);
    assert.ok(!after.includes(`- [x] ${claim}`));
  }
});

test('verified tooling and historical receipt work stay checked and unrelated text is preserved', () => {
  const before = snapshot();
  const after = propose(before).pull_request.body;
  for (const claim of [
    'Inspect current main and existing receipts first; retain the merged validation and regression work.',
    'Guard empty commits.nodes in both initial and paginated gate reads, record a per-case evidence gap, and continue the batch; add regressions for both paths.',
    'Retrieve and validate the required named-test mutation receipts, or reproduce them in isolation; inspect the failed merge-commit verify.py log and resolve or substantiate its relevance.',
    'Empty initial and paginated gate responses record gaps without aborting subsequent cases.',
  ]) {
    assert.ok(after.includes(`- [x] ${claim}`), claim);
  }
  assert.match(after, /^Tracks #1074$/m);
  assert.doesNotMatch(after, /^Closes #1074$/m);
  // The implementation-note excerpt quotes a different PR's old closing text.
  assert.equal(after.includes("The PR body says 'Closes #428'"), true);
  const fingerprint = 'Workflow state fingerprint for Agents Verifier. Do not edit.';
  assert.equal(after.includes(fingerprint), true);
});

test('proposals are idempotent and never claim a remote write was verified', () => {
  const first = propose(snapshot());
  const updated = snapshot();
  updated.issue.body = first.issue.body;
  updated.pull_request.body = first.pull_request.body;
  const second = propose(updated);
  assert.equal(second.issue.body, first.issue.body);
  assert.equal(second.pull_request.body, first.pull_request.body);
  assert.equal(second.remote_metadata_verified, false);
  assert.equal(second.issue.body.split('## Operational acceptance remains incomplete').length - 1, 1);
});

test('unexpected targets and changed checklist shapes are rejected', () => {
  for (const mutate of [
    (s) => { s.repository = 'other/repo'; },
    (s) => { s.issue.number = 1091; },
    (s) => { s.pull_request.number = 1092; },
    (s) => { s.issue.state = 'unknown'; },
    (s) => { s.pull_request.body = s.pull_request.body.replaceAll(reportClaim, 'changed task'); },
    (s) => { s.issue.body = null; },
    (s) => { s.issue.body += '\n<!-- adjudicator-operational-status:end -->'; },
    (s) => { s.issue.body += '\n<!-- adjudicator-operational-status:start -->'; },
  ]) {
    const before = snapshot();
    mutate(before);
    assert.throws(() => propose(before));
  }
});

test('standalone close directives for #1074 are corrected without rewriting other references', () => {
  for (const directive of ['Closes', 'Fixes', 'Resolves', 'closes']) {
    const before = snapshot();
    before.pull_request.body = before.pull_request.body.replace('Closes #1074', `${directive} #1074`);
    before.pull_request.body += '\nCloses #1091\nA quotation of Closes #1074 is unchanged.\n';
    const after = propose(before).pull_request.body;
    assert.match(after, /^Tracks #1074$/m);
    assert.match(after, /^Closes #1091$/m);
    assert.match(after, /A quotation of Closes #1074 is unchanged\./);
  }
});

test('CLI only emits proposed patches and leaves its snapshot byte-identical', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-completion-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const input = path.join(dir, 'snapshot.json');
  const bytes = fs.readFileSync(fixture);
  fs.writeFileSync(input, bytes);
  const stdout = fs.openSync(path.join(dir, 'stdout'), 'w');
  const stderr = fs.openSync(path.join(dir, 'stderr'), 'w');
  try {
    const result = spawnSync(process.execPath,
      [path.join(__dirname, '../scripts/reconcile_adjudicator_completion.js'), input],
      { cwd: dir, timeout: 10000, stdio: ['ignore', stdout, stderr] });
    assert.ifError(result.error);
    assert.equal(result.status, 0, fs.readFileSync(path.join(dir, 'stderr'), 'utf8'));
    const output = JSON.parse(fs.readFileSync(path.join(dir, 'stdout'), 'utf8'));
    assert.deepEqual(output, propose(snapshot()));
    assert.deepEqual(fs.readFileSync(input), bytes);
    assert.deepEqual(fs.readdirSync(dir).sort(), ['snapshot.json', 'stderr', 'stdout']);
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
  }
});

test('merged successor proposals expose closed lifecycle without proposing state changes', () => {
  const before = successorSnapshot();
  const original = structuredClone(before);
  const result = propose(before);
  assert.deepEqual(before, original);
  assert.equal(result.remote_metadata_verified, false);
  assert.equal(result.operational_acceptance_complete, false);
  assert.equal(result.successor.pull_request.expected_merged, true);
  assert.equal(result.successor.pull_request.expected_draft, false);
  for (const key of ['issue', 'pull_request']) {
    const patch = result.successor[key];
    assert.equal(patch.number, key === 'issue' ? 1091 : 1092);
    assert.equal(patch.expected_state, 'closed');
    for (const field of ['state', 'merged', 'draft']) {
      assert.ok(!Object.hasOwn(patch, field), `must not mutate ${field}`);
    }
    assert.equal(patch.expected_body_sha256, createHash('sha256')
      .update(before.successor[key].body).digest('hex'));
    assert.match(patch.body, /issue #1091 is closed; PR #1092 is merged/);
    assert.match(patch.body, /These states do not establish operational acceptance/);
    assert.match(patch.body, /does not authenticate a later open successor/);
    assert.match(patch.body, /historical revision/);
  }
  assert.match(result.successor.pull_request.body, /^Tracks #1091$/m);
  assert.doesNotMatch(result.successor.pull_request.body, /^Closes #1091$/m);
});

test('successor clears unsupported task and acceptance claims in both copies but keeps delivered work', () => {
  const before = successorSnapshot();
  for (const key of ['issue', 'pull_request']) {
    before.successor[key].body = before.successor[key].body.replaceAll('- [ ] ', '- [X] ');
    before.successor[key].body += '\n- [x] Retain delivered inventory tooling.\n';
  }
  before.successor.pull_request.body += '\nCloses #1200\nQuoted Closes #1091 is unchanged.\n';
  const after = propose(before).successor;
  for (const key of ['issue', 'pull_request']) {
    const copies = key === 'issue' ? 1 : 2;
    assert.equal((after[key].body.match(/^- \[ \] /gm) || []).length, 13 * copies);
    assert.doesNotMatch(after[key].body, /^- \[X\] /m);
    assert.match(after[key].body, /^- \[x\] Retain delivered inventory tooling\.$/m);
  }
  assert.match(after.pull_request.body, /^Closes #1200$/m);
  assert.match(after.pull_request.body, /Quoted Closes #1091 is unchanged\./);
});

test('successor proposals are idempotent and do not alter the original proposals', () => {
  const before = successorSnapshot();
  const first = propose(before);
  const legacy = propose(snapshot());
  assert.deepEqual(first.issue, legacy.issue);
  assert.deepEqual(first.pull_request, legacy.pull_request);
  for (const key of ['issue', 'pull_request']) {
    before.successor[key].body = first.successor[key].body;
  }
  const second = propose(before);
  for (const key of ['issue', 'pull_request']) {
    assert.equal(second.successor[key].body, first.successor[key].body);
    assert.equal((second.successor[key].body.match(/## Operational acceptance remains incomplete/g)
      || []).length, 1);
  }
});

test('successor rejects missing provenance, wrong identities and incomplete checklists', () => {
  for (const mutate of [
    (s) => { s.successor = null; },
    (s) => { delete s.successor.issue; },
    (s) => { s.successor.issue.number = 1074; },
    (s) => { s.successor.pull_request.number = 1075; },
    (s) => { s.successor.issue.state = 'unknown'; },
    (s) => { s.successor.pull_request.state = 'unknown'; },
    (s) => { delete s.successor.pull_request.merged; },
    (s) => { delete s.successor.pull_request.draft; },
    (s) => { s.successor.pull_request.state = 'open'; },
    (s) => { s.successor.issue.body = null; },
    (s) => { s.successor.pull_request.body = s.successor.pull_request.body
      .replaceAll('Completion metadata matches the resulting operational evidence.', 'new task'); },
    (s) => { s.successor.issue.body += '\n<!-- adjudicator-operational-status:end -->'; },
  ]) {
    const before = successorSnapshot();
    mutate(before);
    assert.throws(() => propose(before));
  }
});

test('open successor lifecycle is described as captured without implying acceptance', () => {
  const before = successorSnapshot();
  before.successor.issue.state = 'open';
  before.successor.pull_request.state = 'open';
  before.successor.pull_request.merged = false;
  for (const draft of [false, true]) {
    before.successor.pull_request.draft = draft;
    const result = propose(before);
    assert.equal(result.successor.pull_request.expected_draft, draft);
    assert.match(result.successor.issue.body, /issue #1091 is open; PR #1092 is open/);
    assert.equal(result.successor.issue.body.includes('(draft)'), draft);
    assert.equal(result.operational_acceptance_complete, false);
  }
});

test('successor CLI preserves snapshots and emits no partial proposal for invalid input', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-successor-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const input = path.join(dir, 'snapshot.json');
  const output = path.join(dir, 'stdout');
  const errors = path.join(dir, 'stderr');
  for (const valid of [true, false]) {
    const before = successorSnapshot();
    if (!valid) delete before.successor.pull_request.merged;
    const bytes = JSON.stringify(before);
    fs.writeFileSync(input, bytes);
    const stdout = fs.openSync(output, 'w');
    const stderr = fs.openSync(errors, 'w');
    let result;
    try {
      result = spawnSync(process.execPath,
        [path.join(__dirname, '../scripts/reconcile_adjudicator_completion.js'), input],
        { cwd: dir, timeout: 10000, stdio: ['ignore', stdout, stderr] });
    } finally {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
    }
    assert.ifError(result.error);
    assert.equal(result.status, valid ? 0 : 1);
    const emitted = fs.readFileSync(output, 'utf8');
    if (valid) assert.deepEqual(JSON.parse(emitted), propose(before));
    else {
      assert.equal(emitted, '');
      assert.match(fs.readFileSync(errors, 'utf8'), /successor merged state must be captured/);
    }
    assert.equal(fs.readFileSync(input, 'utf8'), bytes);
    assert.deepEqual(fs.readdirSync(dir).sort(), ['snapshot.json', 'stderr', 'stdout']);
  }
});
