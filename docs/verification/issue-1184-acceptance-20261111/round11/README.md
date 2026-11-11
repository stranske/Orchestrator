# CSV operational acceptance, round 11 — 2026-11-11

The regression added to `tests/test_verifier_context_builder_contract.js`
exercises the actual `acquireRetainedExecution` export. Replaying retained
native comparison run, attempt, job and canonical caller metadata reaches the
checkout-log acquisition boundary. An injected transport failure leaves the
scratch directory empty. Replaying the real earlier evaluation artifact as a
mismatched API response rejects at `Retained artifact attempt/origin/size
unproven`, before further acquisition. This verifies recovery admission and
failure behavior; it does not establish native execution or custody.

Fresh GitHub readback confirms PR 1168 remains merged at head
`01e86ae7e4ef0e6a06193c916c8c780ca5cab052` and published merge
`a30b34ada04a30d04360867e14097a8c5b6d731c`. Its 51 native issue comments
still contain no corpus decision. Comparison artifact `13468552385` remains
unexpired. The download connector returned a remote ZIP reference; archive
bytes were not read or authenticated locally. No producer state was created.

Validation: all **15 JavaScript regressions pass**, JavaScript syntax and
`git diff --check` pass, and the corrected standalone CSV contract exits 0
with `contract ok`. Focused pytest with `-m "not slow"` cannot run because
pytest is absent. No Python source changed. Chain evidence exits 2 at
`closer_csv_completion.read_completion` calling `_bytes` on the absent native
PR 989 completion receipt. Missing custody does not establish a checker defect.

The existing comparison recovery dispatch for `42142641060/1/13468552385`
exits 1: `error connecting to api.github.com`. The connector has no workflow
dispatch operation. The worker terminal and stored corrective publication
result, including `coding_terminal_reference`, remain unavailable. Both
remaining acceptance criteria stay unchecked.

The commit attempt exits 128 because `.git/index.lock` cannot be created on
the read-only filesystem. Changes remain uncommitted. The blocker comment and
`needs-human` label are both rejected with `MCP tool call requires approval,
but approval policy is never`. A follow-up runner needs writable Git metadata,
native workflow dispatch and access to actual publication custody.
