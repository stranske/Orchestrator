# CSV acceptance readback, round 6 — 2026-11-11

Progress remains **9/13**. Recent commits and the live PR body were reconciled;
none of the four remaining checkboxes can be completed from current evidence.

The new JavaScript regression invokes the actual fixture reader against six
unchanged native verifier-state comments. All six are admitted for PR 1168's
correct head and published merge. None is admitted when only the head, only the
merge, or the evaluated commit selector is wrong. The alternate identities come
from native PR 989 readback; no decision or publication record is manufactured.
All ten JavaScript tests pass. No Python file changed.

`native-readback.json` retains fresh native identities, original verifier-state
comments, artifact metadata and the rejecting recovery traceback. PRs 1168 and
989 remain merged at their previously recorded heads and published merges.
No trusted corpus decision is published for PR 1168. Recovery run 42142328293,
job 127645488334, rejects the evaluation in `original_final_projection` at
`tools/verifier_event_recovery.py:282`: `Original final producer provenance is
not audited`. Evaluation artifact 13468410186 remains unexpired until
2026-11-13T01:04:54Z. The exact-head retained-output recovery dispatch failed
to connect to `api.github.com` (exit 1); no new run is claimed. Continuation
belongs to the authorized Workflows controller's audited recovery route.
Stored final-head worker terminal/publication custody remains unproven.

`local-checks.json` retains commands, exit statuses and full stdout/stderr:
corrected contract exit 0, pinned partial revision exit 1 with the required
contract AssertionError, fixture self-check exit 0, JavaScript tests and syntax
exit 0, and whitespace check exit 0. Focused pytest with `-m "not slow"` cannot
run because pytest is absent. Chain evidence exits 2 because this runner lacks
the PR 989 receipt: `closer_csv_completion.read_completion` calls `_bytes(path)`
on the absent custody file. This is an availability gap, not a checker defect.

The source test and evidence could not be committed: `git add` exited 128 because
`.git/index.lock` cannot be created on the read-only filesystem. No branch or PR
was created. Attempts to correct the stale PR-body legacy-flag description, add
`needs-human`, and post the concrete blocker comment were each rejected with
`MCP tool call requires approval, but approval policy is never`. The correction
is: historical collection requires `--chain-evidence --legacy`; standalone
`--legacy-chain-evidence` and `--legacy` are rejected. All verified checkboxes
already match the live body. PR 1348 was read back open and ready (`draft=false`).
