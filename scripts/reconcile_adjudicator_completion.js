'use strict';

// Prepare reviewable metadata patches for #1074/#1075 and optional #1091/#1092.
// This tool has no GitHub client and never changes issues, pull requests,
// Brain records or receipts.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');

const pendingClaims = [
  'Reconcile the saved report and switch-review output with the actual run, and correct completion metadata so bounded work cannot imply broad acceptance.',
  'All three named acceptance tests pass on the evaluated source.',
  'Enabling an outcomes write fails the second named test; enabling uncontested offers fails the third; exact restoration passes.',
];
const successorPendingClaims = [
  'Correct completion metadata so delivered tooling is retained without representing operational acceptance as complete.',
  'Recover the original dispute identities and establish their provenance; reconcile against a read-only Brain inventory, recording one persisted shadow result or explicit identity-specific gap per requested dispute.',
  'Diagnose the 16 CLI failures individually. Distinguish stale timeout/schema expectations from cost, ordering and saved-state defects; fix only demonstrated defects or demonstrably obsolete expectations.',
  'Run all three issue-named acceptance tests on the exact evaluated source and perform the two required named-test mutations with byte-identical restoration.',
  'Execute the authenticated retrospective with authoritative outcome writes prohibited and retain case, role-run and cost provenance.',
  'Observe one real contested closer disposition and independently verify its persisted shadow record and unchanged authoritative outcomes.',
  'Reconcile the saved report and actual switch-review output with persisted case and cost records; document historical-object requirements for receipt tests and retain completed target-source CI evidence.',
  'Every authenticated requested dispute maps to a persisted shadow result or an explicit per-case evidence gap; exclusions, missing identities and inventory discrepancies are enumerated.',
  'All three named tests pass on the recorded source revision; outcome-write and uncontested-offer mutations fail the specified named tests, and exact restoration passes.',
  'A real contested closer observation identifies its target, source revision, shadow role record and unchanged authoritative outcomes.',
  'Saved summary and switch-review output match persisted counts and measured or explicitly unknown costs.',
  'Each retained CLI failure has a verified resolution or a specific evidence-backed explanation, with relevant checks passing.',
  'Completion metadata matches the resulting operational evidence.',
];
const start = '<!-- adjudicator-operational-status:start -->';
const end = '<!-- adjudicator-operational-status:end -->';
const status = `${start}
## Operational acceptance remains incomplete

PR #1075 delivered inventory and dispute-identity tooling, historical receipt
validation, synthetic retrospective and closer regressions, and behavioral
evidence for empty-Gate recovery. Keep that delivered work.

The named-test mutation receipts evaluate historical revision
\`6ed980924ec1d7d83a06f9b6a181dc76ba5eb910\`; they do not establish current-source
named-test acceptance. The original authenticated dispute cohort, retrospective
role-run and cost records, real contested closer observation, and saved-report
and switch-review reconciliation remain pending. The retained CLI failures also
need individual diagnosis. Synthetic results do not complete these requirements.

Evidence: [historical receipts](https://github.com/stranske/Orchestrator/blob/main/docs/verification/issue-1074-named-receipts/README.md),
[synthetic retrospective](https://github.com/stranske/Orchestrator/blob/main/docs/verification/issue-1074-retrospective-batch/README.md),
[synthetic closer](https://github.com/stranske/Orchestrator/blob/main/docs/verification/issue-1074-closer-shadow/README.md).
Remaining operational work is tracked in #1091; merging bounded tooling must not
represent #1074 operational acceptance as complete.
The [closer handoff](https://github.com/stranske/Orchestrator/issues/1074#issuecomment-6610044576)
explicitly transferred unmet criteria to #1091. The closed lifecycle of #1074
records that handoff, not successful operational acceptance.
${end}`;

function correctBody(body, claims = pendingClaims, issueNumber = 1074, statusText = status) {
  assert.equal(typeof body, 'string', 'body must be a string');
  const lines = body.split('\n');
  const found = new Set();
  const corrected = lines.map((line) => {
    const match = /^- \[[ xX]\] (.+?)\r?$/.exec(line);
    if (match && claims.includes(match[1])) {
      found.add(match[1]);
      return line.replace(/\[[xX]\]/, '[ ]');
    }
    // Only the standalone closing directive for this source issue is changed.
    const closing = /^(Closes|Fixes|Resolves) #(\d+)\s*$/i.exec(line);
    return closing && Number(closing[2]) === issueNumber ? `Tracks #${issueNumber}` : line;
  }).join('\n');
  assert.equal(found.size, claims.length, `expected #${issueNumber} checklist claims are missing`);
  const first = corrected.indexOf(start);
  if (first < 0) {
    assert.ok(!corrected.includes(end), 'unmatched operational status marker');
    return `${statusText}\n\n${corrected}`;
  }
  const last = corrected.indexOf(end, first);
  assert.ok(last >= first && corrected.indexOf(start, first + start.length) < 0 &&
    corrected.indexOf(end, last + end.length) < 0, 'ambiguous operational status markers');
  return corrected.slice(0, first) + statusText + corrected.slice(last + end.length);
}

function proposeSuccessor(successor) {
  const issue = successor?.issue;
  const pr = successor?.pull_request;
  assert.equal(issue?.number, 1091, 'expected successor issue #1091');
  assert.equal(pr?.number, 1092, 'expected successor PR #1092');
  assert.ok(['open', 'closed'].includes(issue.state), 'unknown successor issue state');
  assert.ok(['open', 'closed'].includes(pr.state), 'unknown successor PR state');
  assert.equal(typeof pr.merged, 'boolean', 'successor merged state must be captured');
  assert.equal(typeof pr.draft, 'boolean', 'successor draft state must be captured');
  assert.ok(!pr.merged || pr.state === 'closed', 'merged successor PR must be closed');
  const lifecycle = `Captured lifecycle: issue #1091 is ${issue.state}; PR #1092 is ` +
    `${pr.merged ? 'merged' : pr.state}${pr.draft ? ' (draft)' : ''}. ` +
    'These states do not establish operational acceptance. ' +
    'This snapshot does not authenticate a later open successor.';
  const statusText = status.replace(end, `${lifecycle}\n${end}`);
  const patch = (item) => ({
    number: item.number,
    expected_state: item.state,
    expected_body_sha256: createHash('sha256').update(item.body).digest('hex'),
    body: correctBody(item.body, successorPendingClaims, 1091, statusText),
  });
  return {
    issue: patch(issue),
    pull_request: { ...patch(pr), expected_merged: pr.merged, expected_draft: pr.draft },
  };
}

function propose(snapshot) {
  assert.equal(snapshot.repository, 'stranske/Orchestrator', 'wrong repository');
  const issue = snapshot.issue;
  const pr = snapshot.pull_request;
  assert.equal(issue?.number, 1074, 'expected issue #1074');
  assert.equal(pr?.number, 1075, 'expected PR #1075');
  assert.ok(['open', 'closed'].includes(issue.state), 'unknown issue state');
  const successor = Object.hasOwn(snapshot, 'successor') ? proposeSuccessor(snapshot.successor) : null;
  const patch = (item) => ({
    expected_body_sha256: createHash('sha256').update(item.body).digest('hex'),
    body: correctBody(item.body),
  });
  return {
    repository: snapshot.repository,
    operational_acceptance_complete: false,
    // These are proposals only. A caller must re-read and verify remote state
    // after applying them before completing any metadata task checkbox.
    remote_metadata_verified: false,
    // The closer transferred remaining work to #1091. Correct claims without
    // reopening that handoff or changing the already-merged PR's lifecycle.
    issue: { number: issue.number, expected_state: issue.state, ...patch(issue) },
    pull_request: { number: pr.number, ...patch(pr) },
    ...(successor ? { successor } : {}),
  };
}

module.exports = { propose };
if (require.main === module) {
  try {
    assert.equal(process.argv.length, 3,
      'usage: node scripts/reconcile_adjudicator_completion.js SNAPSHOT.json');
    const snapshot = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
    process.stdout.write(`${JSON.stringify(propose(snapshot), null, 2)}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
