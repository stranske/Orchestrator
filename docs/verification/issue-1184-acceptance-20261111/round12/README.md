# Native CSV recovery publication checks — 2026-11-11

The new JavaScript regression calls the actual `publishRetainedReport` export
with retained native PR identities, event history and unavailable provider
reports. It verifies that the publisher rejects the superseded evaluation,
a changed head, a changed published merge, and the current comparison's
`UNVERIFIABLE_INPUT` report before calling either the POST callback or the
comment writer. The current comparison reaches report admission as a control;
the changed revisions stop before comment acquisition. No admitted terminal
decision or native producer record is constructed. Test manifests deliberately
omit requirement sources to isolate revision, history and report admission;
this test does not establish requirement or artifact custody.

Validation is retained in `local-checks.json` and the corresponding transcripts:

- All 16 JavaScript regressions pass; syntax and `git diff --check` pass.
- Corrected CSV contract exits 0 with `contract ok` using the repository-selected
  Python. The requested Anaconda interpreter is absent in this runner.
- The pinned partial exporter from revision
  `0507d28acf68d6bf21c68138d18d56cd01968827`, checked by the existing Git-blob
  verification helper and executed only in scratch, exits 1 with
  `AssertionError: CSV contract is not implemented`.
- Focused pytest with `-m "not slow"` cannot run: `No module named pytest`.
- `--chain-evidence` exits 2 because `closer_csv_completion.read_completion`
  calls `_bytes` on the unavailable native PR 989 receipt. This is unavailable
  custody, not an established checker defect. No Python source changed.

Fresh native GET results are summarized in `native-readback.json`. PR 1168 is
still merged at head `01e86ae7e4ef0e6a06193c916c8c780ca5cab052` and published
merge `a30b34ada04a30d04360867e14097a8c5b6d731c`. Its 51 comments contain six
native event states and no native corpus-decision marker. The genuine stored
corrective publication result and `coding_terminal_reference` remain
unavailable. Both remaining acceptance criteria stay unchecked.

GitHub CLI API access fails with `error connecting to api.github.com`. The
previously retained dispatched-seat guard refuses production workflow dispatch
on main; no alternative dispatch transport was used. The GitHub artifact
connector supplied a remote ZIP reference for comparison artifact 13468552385;
its archive bytes were not authenticated locally. The continuation requires an
authorized native recovery of comparison run 42142641060, attempt 1, artifact
13468552385, plus the actual corrective publication custody.

`git add tests/test_verifier_context_builder_contract.js
docs/verification/issue-1184-acceptance-20261111/round12` exits 128:
`Unable to create '.git/index.lock': Read-only file system`. The intended
commit, `test: guard native CSV recovery at final publication`, cannot be
created here. The comment and `needs-human` label attempts on PR 1348 both
return `MCP tool call requires approval, but approval policy is never`.
Changes remain local and uncommitted; no acceptance checkbox was changed.
