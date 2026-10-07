# Named acceptance proof for evaluated merge `6ed9809`

The actual #509 compare run 37594509162 returned CONCERNS/CONCERNS, partly because
its supplied context could not verify the three named pytest nodes and the two
required deliberate faults. This packet closes that narrow evidence gap against
the evaluated merge; source #428 remains OPEN and no provider verdict is rewritten.

The 39-test retrospective/collected-input suite passes. It includes all three
named acceptance tests. Actual production fault 1 writes outcomes.merged in the
retrospective run: the required snapshot test fails at its outcome equality
assertion (exit 1); byte-identical restoration passes (exit 0). Actual fault 2 lets
an adjudicator entry survive without a contested verdict: 3 of 5 named contexts
fail at the offered-role assertion (exit 1), with 2 control contexts passing;
byte-identical restoration passes all 5 (exit 0). No errors or skips occurred.
Tests use private fixture databases and stubbed backends; no real model, Brain,
publication or outcome was changed.

Current evaluated source also contains the contested_verdict binding/filter and
switch_review weekly_line integration. The source/hash locations are retained in
validation.json. Raw console/JUnit/phase argv/cwd and mutation anchors are stored
losslessly in phase-artifacts.json.gz: each original filename maps to base64
bytes plus SHA256. Decode, verify the member hash, and inspect its native format.
The manifest independently hashes both package files.

This does not prove complete persisted-dispute inventory, semantic shadow
adjudication, actual per-case costs or later-truth analysis. Existing #428 recovery
owns those remaining claims. The earlier verification note is chronological;
this dated receipt supersedes its repeated current-pytest-unavailable statements
only for the evaluated revision and tests named here.

The current manifest binds each named file to immutable evaluated merge
`47156fb2e640b6f6421190293a17b4bb60f66b75`, including the README after its
readability repair. Verify `git show <revision>:<path>` bytes, length and SHA256;
this README has since gained this explanation. The earlier unqualified manifest
is preserved verbatim as `manifest-before-readability-repair.json`. Its README
hash predates the formatting repair and must not be applied to later bytes.
