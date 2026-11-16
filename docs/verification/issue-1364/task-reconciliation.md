# PR #1395 task reconciliation — 2026-11-16

Reviewed `973bbdb` and `93041c7`, the retained command exit files and outputs,
and a fresh all-pages connector read of PR #1168 comments. The PR-body update was
attempted but the connector rejected it because approval is required and this
run's approval policy is `never`. Apply the following task disposition when an
authorized writer is available:

- [ ] Authenticate retained comparison bytes for run `42142641060`, attempt `1`, artifact `13468552385`, through the authorized recovery path. The artifact returns 404; the recorded context rejection precedes acquisition.
- [ ] Read the canonical custody index and immutable PR #1168 result, recover `provenance.coding_terminal_reference`, and trace the original worker commit. Those records are not observable on this runner.
- [x] Run `gh api --paginate repos/stranske/Orchestrator/issues/1168/comments` and inspect native decision/head/merge bindings. Commit `973bbdb` retains successful exit 0 and output in `issue-1168-comments.*`; the current connector independently reads 53 comments and no native corpus decision.
- [ ] Run the historical checker when the required genuine records are available, and identify a rejecting function if it disagrees. Those records remain unavailable.

Both acceptance criteria remain unchecked. The completed comments-inspection
task does not establish the required native decision. The two already checked
test-plan command entries remain checked based on their retained executions.

The new publisher pagination regression passes with the other 17 contract tests.
It proves later authentic recovery states are read and prevent publication under
the superseded original event; it grants no recovery or acceptance authority.
