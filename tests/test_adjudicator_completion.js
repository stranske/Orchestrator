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

test('ambiguous status blocks are rejected in either target without changing input', () => {
  const start = '<!-- adjudicator-operational-status:start -->';
  const end = '<!-- adjudicator-operational-status:end -->';
  for (const key of ['issue', 'pull_request']) {
    for (const markers of [
      [end, start, end],
      [start, end, end],
      [start, start, end],
      [start, end, start, end],
      [end, start],
    ]) {
      const before = snapshot();
      before[key].body += `\n${markers.join('\n')}\n`;
      const original = structuredClone(before);
      assert.throws(() => propose(before), /ambiguous operational status markers/,
        `${key}: ${markers.join(' ')}`);
      assert.deepEqual(before, original);
    }
  }
});

test('one existing status block is replaced while surrounding text and lifecycle are preserved', () => {
  for (const state of ['open', 'closed']) {
    const before = snapshot();
    before.issue.state = state;
    for (const key of ['issue', 'pull_request']) {
      before[key].body += '\nUnrelated prefix: résumé.\n' +
        '<!-- adjudicator-operational-status:start -->\n' +
        'Obsolete operational status.\n' +
        '<!-- adjudicator-operational-status:end -->\n' +
        'Unrelated suffix: 続行.\n';
    }
    const result = propose(before);
    assert.equal(result.issue.expected_state, state);
    for (const key of ['issue', 'pull_request']) {
      assert.equal(result[key].expected_body_sha256, createHash('sha256')
        .update(before[key].body).digest('hex'));
      assert.ok(!Object.hasOwn(result[key], 'state'));
      assert.ok(!result[key].body.includes('Obsolete operational status.'));
      assert.match(result[key].body, /Unrelated prefix: résumé\.\n<!-- adjudicator-operational-status:start -->/);
      assert.match(result[key].body, /<!-- adjudicator-operational-status:end -->\nUnrelated suffix: 続行\.\n$/);
      assert.equal(result[key].body.split('## Operational acceptance remains incomplete').length - 1, 1);
    }
    const repeated = propose({
      ...before,
      issue: { ...before.issue, body: result.issue.body },
      pull_request: { ...before.pull_request, body: result.pull_request.body },
    });
    assert.equal(repeated.issue.body, result.issue.body);
    assert.equal(repeated.pull_request.body, result.pull_request.body);
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

test('CLI rejects invalid snapshots without emitting a partial proposal or changing files', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-completion-invalid-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const input = path.join(dir, 'snapshot.json');
  const ambiguous = snapshot();
  ambiguous.pull_request.body += '\n<!-- adjudicator-operational-status:end -->\n' +
    '<!-- adjudicator-operational-status:start -->\n' +
    '<!-- adjudicator-operational-status:end -->\n';
  const invalidTarget = snapshot();
  invalidTarget.repository = 'other/repo';
  for (const [bytes, error] of [
    [JSON.stringify(ambiguous), /ambiguous operational status markers/],
    [JSON.stringify(invalidTarget), /wrong repository/],
    ['{', /JSON/],
  ]) {
    fs.writeFileSync(input, bytes);
    const stdout = fs.openSync(path.join(dir, 'stdout'), 'w');
    const stderr = fs.openSync(path.join(dir, 'stderr'), 'w');
    try {
      const result = spawnSync(process.execPath,
        [path.join(__dirname, '../scripts/reconcile_adjudicator_completion.js'), input],
        { cwd: dir, timeout: 10000, stdio: ['ignore', stdout, stderr] });
      assert.ifError(result.error);
      assert.equal(result.status, 1);
      assert.equal(fs.readFileSync(path.join(dir, 'stdout'), 'utf8'), '');
      assert.match(fs.readFileSync(path.join(dir, 'stderr'), 'utf8'), error);
      assert.equal(fs.readFileSync(input, 'utf8'), bytes);
      assert.deepEqual(fs.readdirSync(dir).sort(), ['snapshot.json', 'stderr', 'stdout']);
    } finally {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
    }
  }
});
