# CSV acceptance readback, round 4 — 2026-11-11

Acceptance remains incomplete. The new JavaScript regression runs the real CSV
fixture assessor against retained native GitHub metadata, six exact-head verifier
states, and PR 1168's native merge acknowledgment. All eight tests pass. The new
case verifies merged state and the acknowledgment's author, URL and head before
requiring missing publication custody and an incomplete outcome. Labeling is
present, so missing labels cannot explain the negative result. No worker terminal,
publication result or corpus decision was generated.

Native readback reconfirms these identities without changing either head:

| PR | Final head | Published merge |
|---|---|---|
| 1168 | `01e86ae7e4ef0e6a06193c916c8c780ca5cab052` | `a30b34ada04a30d04360867e14097a8c5b6d731c` |
| 989 | `85adde2614db7118d6e6d28dd11e5bc4c7611319` | `97647baf97085724af7cb8f684af2331f73fb612` |

Both are merged. Earlier unmerged reports were historical observations. The first
task is verified, and the conditional merge task requires no action. The three
native verifier attempts still have incomplete states; none supplies a
`verifier-corpus-decision/v1`. An attempted `agents-verifier.yml` dispatch on
`main`, pinned to PR 1168's head and selecting retained run 42141057834, attempt 1,
artifact 13468410186 through `verification_recovery`, failed to connect to
`api.github.com`. No new workflow execution is claimed.

Historical source inspection at
`bc077c6f27efc4ddd46a922bf4361bd7ae61b4df^:src/closer_coding_publication_io.py`
confirms `retain_confirmed_result` requires publication ACK/readback custody,
copies `scope.history["terminal_reference"]` into
`provenance.coding_terminal_reference`, and retains a content-addressed
`publication-result-<sha256>.json`. This runner has no accessible result or worker
terminal for the final corrective head. The earlier worker's reported ancestor
pushes and final commit's co-author do not supply that link. No redundant worker
was started. The native merge acknowledgment proves a different fact from stored
publication custody.

`local-checks.json` and the stdout/stderr files retain corrected contract exit 0,
isolated pinned partial exit 1 with the contract AssertionError, self-check exit
0, eight JavaScript tests passing, and chain-evidence exit 2. The chain reader
fails in `closer_csv_completion.read_completion` at `_bytes(path)` because the
PR 989 receipt is absent. This establishes unavailable custody, not a demonstrated
checker defect. Anaconda is unavailable; the repository regression selected the
suite Python. Focused pytest with `-m "not slow"` cannot run because pytest is
absent. No Python file was changed; the Black pre-commit gate does not apply.
`git diff --check` passes.

The required commit failed before staging: Git cannot create `.git/index.lock`
on the read-only mount. The source test and this bundle remain uncommitted on the
checked-out branch. No branch or PR was created. The proposed verified checkbox
updates are retained in `pending-pr-body.md`; the live update, blocker comment and
`needs-human` label were rejected with `MCP tool call requires approval, but
approval policy is never`. Native decision and custody criteria remain unchecked.

Final native readback found PR 1348 open and ready (`draft=false`), with remote
head `62da2426143e605f74975b52aec92820a9e686bd` (message: `docs: retain local native
CSV acceptance and exact blockers`). The local checkout remains at `edbc933c`.
That independently advanced remote commit is not attributed to this run, and no
push or overwrite was attempted.
