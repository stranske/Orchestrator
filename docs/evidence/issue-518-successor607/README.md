# Root #518 successor #607 measurement

This repository-local preparation serves existing root #518 and generated
successor #607. Historical #519 evidence and all generated branches are intact.
Repository pins are never replaced by the retained proposed pins.

The proposed-bundle `report.json` and `provenance.json` describe the actual
corrected strict-nine collector at source `65fb9b0bb846bec44160a0ba1dca0e22e6eeffbc`.
They record nine matching installed versions, zero measured blockers and 3464
collected tests on that historical source. The initial measurement was against
3da6bb95; it is retained in this PR's commit history, not the final report.
The `current-pins/` receipt is an earlier actual current-bundle observation at
b0aa0da4, before strict-nine preflight was added; it does not claim execution
of the repaired collector on that old bundle.

`commands.json.gz` retains complete text argv/cwd/exit/stdout/stderr. Binary
`git archive` stdout is represented by exact count/SHA-256 and retained full-byte
run receipt plus immutable source tree, avoiding recursive archive embedding.
`commands-integrity.json` independently binds compressed/uncompressed records
and the original complete capture. No tool diagnostic is omitted.

Native findings led to complete-nine marker matching and actual nine-package
preflight before measurements, preserving existing real CLI checks. Original
baseline and strict-nine RED logs, marker unknown/incomplete/hybrid controls,
and final 21 focused GREEN checks are retained. One executed regression node
raised the historical3463 floor to3464 without changing any ceiling.

The integration receipt under `integration-20261016/` preserves new main's3466
floor plus that executed node. Actual collection is3467; the21focused checks
pass. This replaces the old floor during rebase, without weakening main.
Full current-head hosted tests, execution mirror, combined coverage, review,
canonical guarded merge, actual native verification and Maint52/71 regenerated
acceptance remain mandatory. Zero coverage-startup blockers prove startup only.
