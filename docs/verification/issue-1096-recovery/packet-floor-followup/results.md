# Packet floor diagnostic results

Source commit `b2151b6912b8c36e76013fb36d186b1f2fa67557`, parent
`8bef6a28fce7b4eecbaa3fc08016d7bb505742b5`, adds per-attempt packet evidence
observations to the read-only retained-report inventory. The observation explains
missing or invalid diff text/revision, clipped patches, missing or invalid
criteria, and serialized qualification. It binds each packet by JSON-value hash
and location while omitting packet contents. Parent and retry packets remain
separate. Saved metadata flags cannot override the content-derived observation.
A complete packet remains unverified for native admission; neither observations
nor matched manifests authorize dispatch or add a canonical grade.

The three new regressions failed before implementation. All 119 focused checks
pass afterward, including a differential check against the existing Python
`_row_metadata_only` rule across 37 packet shapes and a CLI/manifest check.
JavaScript syntax and whitespace checks pass. No Python files changed. The
[validation receipt](validation.json) binds source and regression log hashes.

The [pre-execution plan](plan.md) preceded the [required-test attempt](acceptance-run/checks.json).
The four named tests and full retrospective suite on the identified checkout
both fail before collection: Python 3.14.8 has no pytest. This source revision is
not an identified accepted deployment. The checked task claiming required-test
execution does not establish the acceptance criterion that those tests pass.

The same earlier historical Dropbox backup was refetched. The
[acquisition summary](historical-acquisition.json) records only field counts:
58 rows, 64 recursive attempts, 46 recorded packets, 10 patches with nonempty
text, four marked clipped, and no nonempty criteria arrays. These are direct
observations of extracted historical fields, not execution of the new CLI on
the original bytes. Raw report and packet text were not saved. Extractor text
does not establish original byte identity. No original requested retry manifest
was recovered or reconstructed; this backup is not the later immutable native
source. Exact-name searches did not recover `579-original-results.json`.

No operational task is newly complete. Authenticated native receipts,
independent current truth, original retry and fixed next-20 manifests,
accepted/deployed/mirror bindings and live writer ownership remain unavailable.
No provider call, canonical publication, production write or mirror sync occurred.

Checkout Git metadata is read-only. The source commit is stored in the isolated
Git directory recorded in validation.json; the reviewable handoff is
[the patch](../../../../artifacts/issue-1096-packet-floor.patch). The checkout
branch was not advanced. The connector rejected the blocker comment and
`needs-human` label because writes require approval and run policy is `never`.
Neither remote update was applied.
