# CSV native comparison recovery, round 10 — 2026-11-11

Recent commits through `934752a5` and the live PR 1348 body were reconciled
before continuing. All nine verified checklist items are already checked;
the four native-evidence items remain incomplete. The attempted body update
only corrects the historical-reader flags to `--chain-evidence --legacy`.
The connector rejected that update because approval is required and this
run's approval policy is `never`.

Fresh native GETs retain comparison run 42142641060, exact attempt 1, its
complete six-job inventory and all step results. The comparison evaluator
and artifact upload succeeded; the separate `Run LLM evaluation` step was
skipped. The native pending and incomplete event comments remain unchanged,
and none of the 51 PR 1168 comments contains a native corpus decision.

The added JavaScript regression passes those native run/attempt/job responses
to the actual `reconcilePendingExecution` export. It confirms the exact result
`prior_evaluator_started_collect_existing_outputs`, `retryable=false`, and
`output_names=["comparison.json"]`. This prevents the skipped evaluate-mode
step from authorizing another comparison. It does not claim a native recovery
execution or turn the successful workflow conclusion into a corpus decision.

Artifact 13468552385 remains unexpired, named
`comparison-results-42142641060`, with native metadata digest
`sha256:2ebfd872ed901d067ddbac7990709aa23d7d46d9c03d4150e9fe091bb21dfcba`.
The download connector returned a remote ZIP reference; this runner has not
read or hashed its bytes. Native checkout logs identify helper and reusable
workflow commit `2d47d9742a24246022029dc0a2aaf9ff25e60bb0`. Fresh ancestry
readbacks confirm it and caller commit `93154fe94e4cdf5198697a244da1312765e91649`
are merged ancestors of their respective canonical branches. These are origin
facts, not proof that retained output passes the current producer validator.

The existing recovery dispatch was attempted with `mode=compare` and integer
selectors `42142641060/1/13468552385`; it exited 1 because the shell cannot
connect to `api.github.com`. No new run was created. The GitHub connector has
no workflow-dispatch operation.

Reinspection of the retained `retain_confirmed_result` source confirms it
requires actual publication execution, ledger readback, a canonical index bind
and immutable custody readback. Its `coding_terminal_reference` comes from
`scope.history["terminal_reference"]`. This runner still lacks the stored
corrective result and that terminal reference. Existing ancestor pushes and
the native merge acknowledgment cannot supply them; no redundant worker or
replacement record was created.

Validation output is retained in `local-checks.json`: all **14 JavaScript
regressions pass**, syntax and diff checks pass, and the corrected CSV contract
exits 0. Chain evidence exits 2: `closer_csv_completion.read_completion` calls
`_bytes` on the unavailable native PR 989 completion receipt. This is missing
custody, not an established checker defect. Focused pytest with
`-m "not slow"` is unavailable (`No module named pytest`). No Python source
changed, so the conditional Black gate does not apply.

The required commit cannot be created: `git add` exits 128 because
`.git/index.lock` cannot be created on the read-only filesystem. Source and
evidence changes remain uncommitted. The blocker comment and `needs-human`
label were rejected with `MCP tool call requires approval, but approval policy
is never`. PR 1348 remains open and ready for review. Its remote head advanced
to `76d77372758b5a0f274401fdc93d2cb9e084dff5` during the run; the local checkout
remains at `934752a5`. This run did not publish or change either historical
corrective head, workflows, or native producer records.
