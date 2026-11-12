# CSV operational acceptance, round 8 — 2026-11-11

Progress remains **9/13**. Commits through `950b66e0` and the live PR 1348 body
were reconciled before continuing. The nine checked items already reflect the
verified implementation; none of the four native evidence items can be checked.

The retained round-7 dispatch contains quoted recovery IDs. The actual exported
`parseRecoverySelector` requires positive safe integer IDs, so that request would
fail admission even after transport is restored. The new JavaScript regression
reproduces that failure, admits the corrected command below, and binds its three
IDs to the unchanged native recovery comment and original evaluation artifact.
It also checks each mixed string/integer case. No production parser, workflow,
producer record, decision, or publication custody was changed or manufactured.

```
gh workflow run agents-verifier.yml --repo stranske/Orchestrator --ref main \
  -f pr_number=1168 \
  -f pr_head_sha=01e86ae7e4ef0e6a06193c916c8c780ca5cab052 \
  -f mode=evaluate \
  -f 'verification_recovery={"run_id":42141057834,"run_attempt":1,"artifact_id":13468410186}'
```

The corrected dispatch exited 1: `error connecting to api.github.com`. No new
native run is claimed. The available GitHub connector has no workflow-dispatch
operation. Fresh native comments still contain six event states and no terminal
corpus marker for PR 1168. The retained artifact remains unexpired until
2026-11-13T01:04:54Z. Fresh job-log readback confirms the existing recovery failed
in `original_final_projection` at `tools/verifier_event_recovery.py:282` with
`Original final producer provenance is not audited`. `native-readback.json`
retains the exact traceback window, native artifact metadata, recovery comment,
unchanged PR heads and published merges, and attempted dispatch argv/output.
Resolving that producer audit belongs to the authorized Workflows recovery owner.

Inspection of the retained `retain_confirmed_result` source confirms its
`provenance.coding_terminal_reference` comes from `scope.history["terminal_reference"]`
after publication ACK and native readback. The available PR 1168 Codex log
retains only ancestor pushes (`d7502402`, `860c9125`), not the final corrective
head. This cannot supply the missing stored result or terminal reference. No
redundant worker or invented record is used to fill that gap.

Validation:

- `node tests/test_verifier_context_builder_contract.js`: **12/12 pass**.
- Standalone corrected contract using the repository Python: **0**, `contract ok`.
- Chain evidence: **2**, missing native PR 989 receipt in this runner. The failing
  read remains `closer_csv_completion.read_completion` calling `_bytes(path)`;
  this establishes unavailable custody rather than a producer/checker mismatch.
- JavaScript syntax and `git diff --check`: pass.
- Focused pytest with `-m "not slow"`: unavailable (`No module named pytest`).

Full available command output is retained in `local-checks.json`. No Python
source changed, so the conditional Python formatting gate does not apply.

The PR-body correction, blocker comment, and `needs-human` label were rejected:
`MCP tool call requires approval, but approval policy is never`. The stale body
sentence should say historical collection requires `--chain-evidence --legacy`;
standalone legacy flags are rejected. No checkbox changes are proposed.

The required source/test commit is blocked: `git add` exited 128 because
`.git/index.lock` cannot be created on the read-only filesystem. The test and
evidence remain uncommitted. No alternate publication transport or approval
bypass was attempted.
