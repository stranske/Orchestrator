# CSV acceptance readback, round 5 — 2026-11-11

Acceptance remains incomplete. No task or acceptance checkbox was completed.
The new regression runs the actual fixture reader against the retained native
PR 984 corpus decision and PR 1168 verifier records. It accepts the partial
decision for its own pinned head and merge, excludes it from the corrective
result, and confirms that two successful workflows cannot replace the missing
PR 1168 decision. All nine JavaScript tests pass; full output is retained here.
The corrected CSV contract also exits zero with `contract ok`. No Python file
was modified, so the Black pre-commit gate does not apply.

Fresh native readback reconfirms both historical PRs merged at their recorded
heads and published merges. PR 1168 has 51 returned comments, including six
native event-state comments and no corpus decision marker. PR 984's genuine
corpus decision is in comment 6458980062, which is already retained in
`tests/fixtures/verifier_native_bot_origin/canary984-selected-origin.json`.
The misleading opening outcome sentence in the earlier local readback is
corrected to agree with that readback's detailed incomplete findings.

The failed recovery job 127645488334 in run 42142328293 identifies the first
remaining blocker precisely: `original_final_projection` in
`tools/verifier_event_recovery.py:282`, called by `revalidate_retained:485`,
raises `ValueError: Original final producer provenance is not audited`.
Its native traceback and job steps are retained in `native-readback.json`.
`Revalidate authenticated retained output` failed and lifecycle publication
was skipped. This does not establish a checker defect or justify relaxing
the producer provenance check.

Evaluation artifact 13468410186 from run 42141057834 remains available with
digest `sha256:c82d28c792185972e8897e057b0398e7ddb0a73ea0ecc95c96306ecd01b4ae82`
and expiry 2026-11-13T01:04:54Z. A dispatch selecting that retained evaluation
through the existing `verification_recovery` input failed to connect to
`api.github.com` (exit 1). No new run or provider evaluation is claimed. The
continuation owner must resolve the audited producer provenance through the
native Workflows recovery route before publishing a decision for PR 1168.

Final-head worker terminal and stored `retain_confirmed_result` custody remain
unavailable here. The existing historical source and worker extracts were
inspected; ancestor pushes and a merge acknowledgment do not fill that gap.
No redundant worker was started or evidence fabricated.

The current chain command exits 2 because this runner lacks the PR 989 receipt
at `/home/runner/.codex/orchestrator/closer-core/completions/stranske_orchestrator_988_989.json`.
The exact rejecting read is `closer_csv_completion.read_completion` calling
`_bytes(path)`. This is unavailable custody, not proof of a checker defect.
Previously retained local PR 989 success does not establish PR 1168 acceptance.

No workflow, synced script, CSV fixture or label on the historical PRs was
changed. PR 1348 was read back open and ready (`draft=false`).

The required commit could not be created: `git add` failed with
`Unable to create '.git/index.lock': Read-only file system` before staging.
The source regression and this evidence remain in the working tree. Attempts
to add `needs-human` and post the concrete blocker comment to PR 1348 were both
rejected with `MCP tool call requires approval, but approval policy is never`.
No approval bypass, alternate publication or push was attempted.
