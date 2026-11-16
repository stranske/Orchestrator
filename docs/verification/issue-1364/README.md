# Recover native verifier and worker publication records — 2026-11-15

Follow-up to [stranske/Orchestrator#1364](https://github.com/stranske/Orchestrator/issues/1364)
after merged PR #1348. This round records the authorized recovery path, fresh native
readback, publication-custody lookup, and the issue-named checks without redoing the
#1348 contract regressions or shard work.

## Authorized retained comparison recovery

Source selectors (unchanged from #1184 / round 9):

```
run_id=42142641060, run_attempt=1, artifact_id=13468552385
head=01e86ae7e4ef0e6a06193c916c8c780ca5cab052
evaluated_merge=a30b34ada04a30d04360867e14097a8c5b6d731c
```

**Prior authorized execution (outside this seat).** Production workflow dispatch
[43133282976](https://github.com/stranske/Orchestrator/actions/runs/43133282976)
(2026-11-12) used `mode=compare` and the integer recovery selectors above. The job
failed at step **Revalidate authenticated retained output** with:

```text
ValueError: Retained assessment is not applicable to current immutable request
```

Native PR #1168 comments now include matching `verifier-event-state/v1` markers for
`run_id=43133282976` (`pending`, then `incomplete` with
`failure_cause=retained_revalidation_before_publication`). No
`verifier-corpus-decision/v1` was published.

**This seat (2026-11-15).** A fresh `gh workflow run agents-verifier.yml` on `main`
with the same recovery JSON is refused by the orchestrator gh guard (production dispatch
is controller-owned). No bypass was attempted.

**Retained artifact bytes.** `GET .../actions/artifacts/13468552385` returns HTTP 404
today. Prior readback recorded digest
`sha256:2ebfd872ed901d067ddbac7990709aa23d7d46d9c03d4150e9fe091bb21dfcba` and expiry
`2026-11-13T01:19:54Z`; authentic byte authentication is therefore unavailable on this
runner.

Disposition: recovery is blocked by **expired/missing artifact custody** and the prior
run's **immutable-request revalidation rejection**, not by a missing workflow definition.

## Publication custody (`retain_confirmed_result`)

`closer_coding_publication_io.retain_confirmed_result` stores
`publication-result-<sha256>.json` and binds `provenance.coding_terminal_reference` from
`scope.history["terminal_reference"]` (see
`docs/verification/issue-1184-acceptance-20261111/round3-local/retain-confirmed-result.txt`).

Searches under `$ORCH_STATE_DIR`, `$ORCH_LOCAL_RUNTIME`, and
`~/.codex/orchestrator` found **no** `publication-result-*.json` for corrective head
`01e86ae7`. GitHub confirms PR #1168 merged at that head and merge
`a30b34ada04a30d04360867e14097a8c5b6d731c`, but the immutable producer record and
terminal reference are not observable here. A merge acknowledgment or event-state comment
is not `retain_confirmed_result` custody.

## Issue-named commands

| Command | Exit | Outcome |
| --- | ---: | --- |
| `gh api --paginate repos/stranske/Orchestrator/issues/1168/comments` | 0 | 53 comments; **zero** `verifier-corpus-decision/v1` bound to the required head/merge |
| `/opt/anaconda3/bin/python3 tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py --chain-evidence --legacy` | 2 | `completion: evidence incomplete`; missing native corpus + stored publication custody |

`--chain-evidence --legacy` does **not** identify a rejecting checker function versus
genuine records: exit 2 reflects **unavailable** custody and incomplete evidence, not a
checker disagreement with authentic inputs (see `chain-evidence.stdout.txt`).

Fresh structured readback is in `native-readback.json`. Raw comment pagination is in
`issue-1168-comments.json`.

## Acceptance criteria disposition

Both acceptance criteria remain **unchecked**:

1. No native `verifier-corpus-decision/v1` on issue/PR #1168 comments for the required
   head and evaluated merge SHAs.
2. Worker terminal, immutable producer result, canonical index binding, and native
   publication readback do **not** jointly establish the corrective commit on this runner;
   chain evidence stays incomplete.

Confirmed access failures (artifact 404, dispatch guard, missing local custody) change
the disposition to **parked**: controller-owned recovery or historical custody surfacing
is required before the criteria can pass. No code under `src/` or `tests/` was changed.