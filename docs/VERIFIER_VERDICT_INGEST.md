# Fleet verifier verdict evidence

The consumer `agents-verifier.yml` calls the Workflows reusable verifier. In compare mode,
`reusable-agents-verifier.yml` lines 1258–1272 runs `tools/verifier_corpus_evidence.py`
before posting `comparison-comment.md` to the merged PR. That helper appends a
`verifier-corpus-decision/v1` JSON marker only when every provider supplied a usable LLM
verdict and the merge-CI context is explicit. The marker binds the repository, PR number,
PR head SHA, evaluated merge SHA, workflow run, and attempt. `PASS` means every provider
passed and merge CI did not fail; `NON_PASS` means a provider or merge CI failed. A green
CI check, a provider row in the prose report, or a successful verifier job is not a
verifier verdict.

`verifier_evidence.py` reads the newest exact-identity marker from a GitHub Actions PR
comment. Missing, malformed, or inaccessible evidence remains unknown. The keepalive
ingest records it for newly seen merged PRs, and the durability sweep fills NULL
verdicts even after durability has resolved. The Brain retains its separate durability
classification; a `NON_PASS` verdict prevents a durable row from counting as success.
