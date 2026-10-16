# Source 579: inherited supporting-source transport

A qualified retrospective case can now include `supporting_sources` in its qualification JSON. These descriptors stay separate from the exact first-parent changed-path inventory. Each descriptor supplies `path`, `evaluated_sha`, the Git `blob_sha`, the full-file `sha256`, and `line_ranges` (an empty list selects the full file; otherwise ordered, disjoint inclusive line pairs select explicit excerpts).

The collector recollects regular immutable Git blobs without network access, checks both hashes and the evaluated revision, and rejects duplicates, changed-path collisions, unsafe paths, injected contents/flags and invalid ranges. Excerpts retain full-blob identity and individual text hashes while declaring `exhaustive=false`. The existing complete ASCII-rendered prompt ceiling still applies.

The collector also computes `material_evidence_revision` over the collected packet and owned UNKNOWNs. Checkout location and unused snapshot transport fields are excluded from that content identity; supporting descriptors are ordered canonically. The revision is evidence identity only: the collector capability remains semantic-only, `persisted_admission=false`, and serialized packets do not gain authority to retry or grade. The reporting floor also refuses serialized qualification packets even with full patch/criteria fields or a forged persisted-admission flag; the original unqualified diff-and-criteria contract remains unchanged.

## Original-case check

The retained original case `44e85dfcdd64335e346a7132a927917e61846910b9ca04e81e346a654c5e88a5` for PR478, evaluated commit `1264f68b8df9277ea0b4aa1cfe554d1848099789`, was recollected locally. Its four original changed paths remain unchanged; seven inherited sources (three full files and four explicitly excerpted files) now fit the actual role prompt: 224582 UTF-8 bytes, below1048576. Material revision: `661ef016350a29a32692394ec17221f5a76e383e7c7a220454225ac2eac39f29`. No provider dispatch, original-report replacement, Brain mutation, or native grade was performed. Historical invocation authenticity and external exhaustiveness remain UNKNOWN.

## Validation and remaining acceptance

The focused adjudicator suites pass. Six separate deliberate breaks remove the serialized-qualification reporting floor, remove the hash check, allow a changed-path collision, allow out-of-bounds excerpts, include checkout location in material identity, and retain changed-inventory presentation order in that identity. Each named regression fails under its break and passes after byte-identical restoration. The exact collected floor rises3494→3523 by29 new executed controls; every ceiling is unchanged. This is a union floor, not a claim of a new full-suite local execution.

Source579 stays open. The next bounded dependency is native revision-gated retry/persistence: carry the collector-issued capability through the native boundary, refuse same-revision duplicate dispatch, and preserve the canonical primary plus every historical paid attempt before any side effect. Any future grading must retain the original cohort, evidence floor, authentic cost requirements, and October18/November3 acceptance contracts. This transport change does not satisfy that native dependency or the source's overall acceptance.
