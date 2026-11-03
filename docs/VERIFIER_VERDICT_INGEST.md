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

`verifier_evidence.py` projects the current exact-identity event from authenticated
GitHub Actions PR comments. Missing, malformed, or inaccessible evidence remains unknown. The keepalive
ingest records it for newly seen merged PRs, and the durability sweep fills NULL
verdicts even after durability has resolved. The Brain retains its separate durability
classification; a `NON_PASS` verdict prevents a durable row from counting as success.


## Retained reviews and incomplete history

The optional `verification_recovery` workflow input contains only an Actions run ID,
run attempt and artifact ID. The reusable verifier authenticates those selectors,
revalidates retained output against the current request and its immutable inputs,
and uses the normal publication and verdict lifecycle without another model call.
The original execution receipt and result remain historical evidence; a separate
revalidation receipt identifies the source artifact and current validation.

Current status follows authenticated event ancestry rather than simply choosing
the highest workflow run ID. An older independent chain that ended incomplete and
never produced a semantic verdict does not invalidate a later complete review with
a valid payload/execution-receipt admission marker of
the same repository, PR, head, merge and original requirements. Every historical
record remains retained. Actual verdict conflicts, changed requirements, newer
pending or incomplete attempts, malformed events and missing predecessors still
prevent an unsupported PASS. A new pending attempt of the selected event or its
explicit successor masks the previous terminal outcome until that attempt settles.


Malformed evidence with a known run and attempt is retained as a non-voting
observation, including its source URL and raw marker hash. It masks older or
same-attempt results, but a later authenticated repair can supersede it. The
existing completion history retains these observations so delayed old inputs
cannot resurrect a stale PASS or erase a newer repair. Evidence whose identity
cannot be ordered remains unknown until its provenance is recovered.
