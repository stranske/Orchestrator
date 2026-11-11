# CSV operational acceptance, round 7 — 2026-11-11

Progress remains 9/13. Review of commits through `8e9f4b15` and the live PR 1348
body found all verified checkboxes already reconciled. The four remaining items
still require native verifier completion and final-head worker/publication custody.

The GitHub connector returned 119 normalized timeline records for PR 1168,
including issue comments, inline review comments and reviews. Six records mention
`verifier-corpus-decision/v1` in discussion; none supplies a native corpus decision.
The retained native evaluation and comparison reports both have the headline
`UNVERIFIABLE_INPUT`. Provider-table `CONCERNS` values in the comparison report do
not override that headline or establish a corpus decision. `native-readback.json`
retains the two unchanged report bodies, original authors and URLs, and the URLs
of the discussion mentions. No producer decision or publication record was created.

The added JavaScript regression invokes the actual Python reader on both native
reports. It first verifies that the reader admits their authors and PR comment
URLs, then checks both headline verdicts and empty corpus decisions. This guards
against mistaking a provider comparison table for the missing terminal decision.
The existing regression suite passes all 11 tests:

```
node tests/test_verifier_context_builder_contract.js
```

The corrected standalone contract prints `contract ok` and exits 0 using the
repository-selected Python. JavaScript syntax and `git diff --check` pass.
No Python source changed; the Python formatting gate does not apply. Pytest is
unavailable in this runner (`python3 -m pytest --version`: No module named pytest).

The authorized exact-head retained-output recovery dispatch failed to connect
to `api.github.com` with exit 1; its full argv and output are retained in the
readback. No new native run or decision is claimed. The first producer rejection
remains the previously retained `original_final_projection` provenance audit
failure. Continuation requires the authorized Workflows recovery owner to resolve
that audit, preserving review, publication and retry gates.

Fresh native PR readback confirms that PRs 1168 and 989 remain merged with the
same final heads and published merge SHAs. No merge is applicable. The final
1168 commit message credits Cursor, whereas the previously retained Codex worker
log contains only ancestor pushes. Commit metadata does not supply the missing
worker terminal or `retain_confirmed_result` custody, so no custody item is checked.

The attempt to correct the stale PR-body legacy-flag description was rejected:
`MCP tool call requires approval, but approval policy is never`. Historical
collection requires `--chain-evidence --legacy`, as verified in earlier rounds;
the standalone legacy flags are rejected. This PR-body correction would change
no task checkboxes.

The source/test commit could not be created: `git add` exited 128 because
`.git/index.lock` cannot be created on the read-only filesystem. The test and
evidence remain uncommitted. The blocker comment and `needs-human` label were
also rejected with `MCP tool call requires approval, but approval policy is never`.
No alternative publication transport or approval bypass was attempted.
