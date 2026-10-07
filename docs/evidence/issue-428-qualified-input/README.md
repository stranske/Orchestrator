# Issue 428: qualified retrospective semantic input

This extends the existing collector and role; the broad issue stays open.
Fresh first-parent diff contents, full criterion provenance and ordered collected
artifact/owned-UNKNOWN mappings issue an opaque in-process input. Serialized JSON
and persisted reports retain their evidence floor. Shadow judgment is separate
from native authenticity, cost, later truth and installed publication.

## Reproduce the focused gates

From a checkout with the repository development prerequisites:

```sh
python3 -m pytest -q tests/test_adjudicator_qualified_input.py tests/test_adjudicator_collected_input.py tests/test_adjudicator_retro_collection.py tests/test_adjudicator_retro.py tests/test_adjudicator_retro_evidence.py
python3 -m mypy src/roles.py src/adjudicator_retro.py
python3 src/adjudicator_retro.py --selftest
python3 src/roles.py --selftest
```

The retained run passed 158 tests plus 23 subtests, mypy and both module selftests.
Eight actual production mutations separately removed digest integrity, complete
inventory equality, body provenance hash, retrospective isolation, full finding
contents, prompt budget, cited evidence membership and before-source inclusion.
Each selected test produced one assertion failure (exit 1), followed by a
byte-identical restoration and a passing selected test (exit 0). The first
compaction experiment did not remove nested contents and stayed GREEN; it is
retained as an incomplete attempt, not counted as proof. Raw logs, JUnit, argv,
hashes and both experiment scripts are in `validation.tar.gz`, bound by
`validation-index.json`. Historical absolute argv identify the original run;
use the current checkout and a new output directory when replaying mutations.

## Actual collected packet boundary

A dry run freshly collected Orchestrator#445 at evaluated merge
74fca179ede178f6d655dfd662ef3ce6081cc765 against its exact first parent.
All 24 changed source paths, 18 declared artifact slots and all three full
acceptance criteria were carried in the complete prompt under an explicit
4 MiB budget. The saved inventory and PR snapshot hashes are retained. This is
collection and transport evidence: no backend was dispatched, no Brain role
record was written and no saved historical report was changed. The result was
`baseline_needs_more_evidence`, with `semantic_judgment_only` qualification and
`persisted_admission=false`. Existing source-sensitive Sol assessment
910d10da is historical design context, not an authentic native adjudicator run.

The fake native runner test proves integration, isolation and source tagging
only. Actual native adjudication receipts, measured costs, later durability,
snapshot authenticity and external inventory exhaustiveness remain UNKNOWN.
The direct CLI requires new output paths and never promotes serialized reports.
Private checkout and scratch mirror verification are recorded separately in
the PR; passing these checks does not publish the installed mirror.
