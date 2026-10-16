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
`git archive` stdout retains exact count/SHA-256. The transferable
`measurement-sources.bundle` retains the exact bound source commits and trees
(including pre-rebase heads), so every archive can be regenerated from its
recorded argv. Run `git fetch ./measurement-sources.bundle "refs/evidence/issue-518/*:refs/evidence/issue-518/*"` from a clone with current main. Bundle verification checks its reachable-main prerequisites; `measurement-sources-integrity.json` binds its bytes and source refs. Absolute local paths in historical records are historical storage locations; recovery uses this bundle.
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

The integrated source was also actually run through the corrected strict-nine collector under the proposed bundle. `integration-20261016/proposed/` binds source133fec07, all nine matching actual versions, zero blockers and3467collection. Full text commands are lossless compressed and binary archive output is separately hash-bound. The collection listing is losslessly compressed with a structured argv/exit/count/hash receipt; it remains recoverable in full.

Second integration: main advanced to 387e2d364bb2a67f0f4c1472a31183e60c15cd13 (floor3469). Current integration preserves its full floor record and ceilings plus one executed strict-nine regression; expected union3470 is measured separately below. Previous integration-20261016 proof remains immutable historical evidence, not coverage for this newer head.

Final second integration ACTUAL collection3470 and all21 named focused tests PASS, with exact argv/source/exit/full verbose log/count/SHA in integration-second-20261016/focused-invocation.json. This is the current integrated floor (main3469 plus executed regression). Earlier3464/3467 receipts remain dated history.
