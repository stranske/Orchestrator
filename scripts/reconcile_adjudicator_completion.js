'use strict';

// Prepare reviewable metadata patches for #1074/#1075. This tool has no GitHub
// client and never changes issues, pull requests, Brain records or receipts.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');

const pendingClaims = [
  'Reconcile the saved report and switch-review output with the actual run, and correct completion metadata so bounded work cannot imply broad acceptance.',
  'All three named acceptance tests pass on the evaluated source.',
  'Enabling an outcomes write fails the second named test; enabling uncontested offers fails the third; exact restoration passes.',
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

function correctBody(body) {
  assert.equal(typeof body, 'string', 'body must be a string');
  const lines = body.split('\n');
  const found = new Set();
  const corrected = lines.map((line) => {
    const match = /^- \[[ xX]\] (.+?)\r?$/.exec(line);
    if (match && pendingClaims.includes(match[1])) {
      found.add(match[1]);
      return line.replace(/\[[xX]\]/, '[ ]');
    }
    // Only the standalone closing directive for this source issue is changed.
    return line.replace(/^(?:Closes|Fixes|Resolves) #1074\s*$/i, 'Tracks #1074');
  }).join('\n');
  assert.equal(found.size, pendingClaims.length, 'expected #1074 checklist claims are missing');
  const first = corrected.indexOf(start);
  if (first < 0) {
    assert.ok(!corrected.includes(end), 'unmatched operational status marker');
    return `${status}\n\n${corrected}`;
  }
  const last = corrected.indexOf(end, first);
  assert.ok(last >= first && corrected.indexOf(start, first + start.length) < 0 &&
    corrected.indexOf(end, last + end.length) < 0, 'ambiguous operational status markers');
  return corrected.slice(0, first) + status + corrected.slice(last + end.length);
}

function propose(snapshot) {
  assert.equal(snapshot.repository, 'stranske/Orchestrator', 'wrong repository');
  const issue = snapshot.issue;
  const pr = snapshot.pull_request;
  assert.equal(issue?.number, 1074, 'expected issue #1074');
  assert.equal(pr?.number, 1075, 'expected PR #1075');
  assert.ok(['open', 'closed'].includes(issue.state), 'unknown issue state');
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
