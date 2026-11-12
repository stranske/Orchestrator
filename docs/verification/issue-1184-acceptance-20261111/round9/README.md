# CSV operational acceptance, round 9 — 2026-11-11

The old evaluation recovery proposed in round 8 is superseded by the later
native comparison. Integer selector admission alone did not establish history
admission. The actual exported `guardRetainedRecoveryHistory` rejects recovery
of evaluation run 42141057834 with:

```
Retained recovery is superseded by a newer authenticated assessment
```

The new regression exercises that guard against all six unchanged native state
comments. A positive control admits the old recovery before the comparison;
restoring the comparison comments blocks it. Recovery of the latest comparison,
42142641060 attempt 1, passes the history guard. This is only history admission:
artifact acquisition, producer validation, publication and terminal completion
have not been established. No producer state or decision was manufactured.

Fresh native artifact metadata identifies comparison artifact 13468552385,
`comparison-results-42142641060`, unexpired at readback with expiry
2026-11-13T01:19:54Z. Its digest is
`sha256:2ebfd872ed901d067ddbac7990709aa23d7d46d9c03d4150e9fe091bb21dfcba`.
The corrected operational command is:

```
gh workflow run agents-verifier.yml --repo stranske/Orchestrator --ref main \
  -f pr_number=1168 \
  -f pr_head_sha=01e86ae7e4ef0e6a06193c916c8c780ca5cab052 \
  -f mode=compare \
  -f 'verification_recovery={"run_id":42142641060,"run_attempt":1,"artifact_id":13468552385}'
```

Dispatch exited 1, `error connecting to api.github.com`. The connector has no
workflow dispatch operation. No new run, provider call or corpus decision is
claimed. The older evaluation recovery's recorded producer audit failure does
not establish how the latest comparison recovery would resolve.

Recent commits through `083c946c` and the live PR 1348 body were reconciled
before continuing: all nine verified items are already checked. The four native
evidence items remain unchecked. The attempted body correction changes only the
stale legacy-flag description: historical collection requires
`--chain-evidence --legacy`. That mutation was rejected because connector
approval is required and the approval policy is `never`.

Fresh native readback confirms PRs 1168 and 989 remain merged at their previously
recorded final heads and published merges. PR 1168 has six pending/incomplete
native event states and no native corpus marker among the 51 returned issue
comments. The missing final-head worker terminal and stored
`retain_confirmed_result`/`coding_terminal_reference` custody remain unresolved;
the existing ancestor pushes and merge acknowledgment cannot supply them. No
historical PR head, workflow or publication record was changed.

Validation (full output in `local-checks.json`):

- JavaScript regression suite: **13/13 pass**.
- JavaScript syntax and `git diff --check`: pass.
- Corrected standalone CSV contract using repository Python: **0**, `contract ok`.
- Current chain evidence: **2**, missing native PR 989 completion receipt.
  The rejecting read remains `closer_csv_completion.read_completion` calling
  `_bytes(path)`; this is unavailable custody, not proof of a checker defect.
- Focused pytest with `-m "not slow"`: unavailable (`No module named pytest`).

No Python source changed, so the conditional Black gate does not apply.

The required commit is blocked: `git add` exited 128 because `.git/index.lock`
cannot be created on the read-only filesystem. The test and evidence remain
uncommitted. The blocker comment and `needs-human` label were also rejected:
`MCP tool call requires approval, but approval policy is never`. Final native
readback confirms PR 1348 is open and ready for review (`draft=false`). No
alternate publication transport or approval bypass was attempted.
