# Issue 417: batch offering and current acceptance proof

The existing PromptAgent, `run_prompt_batch`, advisor match/decline events,
and dispatcher gate implement this scope. No new capability or event store is
introduced. The `role-prompt` bindings cover `research-program` and
`repo-audit:phase-4`; an explicit batch routes once while retaining a separate
role run for each body. Automatic dispatch remains off when `ORCH_ROLE_SHADOW`
is unset, and the delegate CLI reports that fact once per process.

Single-body, one-prompt, and no-batch decline reasons are non-demotable
`wrong_moment` on both writes and historical reads. The named regression checks
sixty events across spelling/kind combinations and preserves ledger bytes.
Phrases outside this bounded classifier remain governed by the existing policy;
this issue does not claim a general natural-language classifier.

## Current reproducible acceptance

The earlier acceptance run on 2026-10-05, against merge `4f952e3b666b04dd100ed5d698650c3bf7ead87c`,
`/opt/anaconda3/bin/python3.12 scripts/verify_role_prompt_offering.py` completed
all three real pytest controls below. Each private module starts from the
checkout bytes, passes its baseline, fails exactly one named assertion after
mutation, and passes after byte-exact restoration. JUnit counts reject missing
or stale reports, import/usage errors, skips, and unrelated failures. No live
ledger, Brain, mirror, registry, or dispatch state is written.

| Control | Named test in `tests/test_role_prompt_offering.py` | Baseline | Broken | Restored |
| --- | --- | --- | --- | --- |
| Remove research-program consult site | `test_research_program_surface_is_declared_and_binds_role_prompt` | 1 pass, exit 0 | 1 failure, exit 1 | 1 pass, exit 0 |
| Make wrong_moment demotable | `test_wrong_moment_declines_never_demote` | 1 pass, exit 0 | 1 failure, exit 1 | 1 pass, exit 0 |
| Count each body against cycle cap | `test_a_batch_counts_once_against_the_cycle_cap` | 1 pass, exit 0 | 1 failure, exit 1 | 1 pass, exit 0 |

The script is the durable replay surface and prints full JSON receipts. Runner
scratch paths are temporary inputs, not acceptance dependencies. Run it with a
Python environment containing the pinned pytest prerequisites.

That run's focused offering/batch/receipt suites passed 29 cases plus six receipt
subtests, and whole-tree Black passed. Replay with:

```sh
python3 scripts/verify_role_prompt_offering.py
python3 -m pytest tests/test_role_prompt_offering.py tests/test_role_prompt_batch.py tests/test_role_prompt_offering_controls.py -q -m "not slow"
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

Current-head CI remains authoritative for
checkout and mirror-shape verification. Check runner prerequisites on every replay;
neither an earlier successful run nor a missing-tool failure establishes
current-head readiness.

## Current keepalive reconciliation

The four implementation tasks landed in `4f952e3`; the two later commits repaired
the diagram and added its regression. This round verified the existing behavior
with production imports and private-ledger/Brain checks using Python's standard
library. The required named pytest run remains blocked by missing prerequisites.

- [x] `research-program` is a declared consult caller; both requested surfaces bind
  `role-prompt` with batch reasons and offer it even when classification misses.
- [x] Historical and newly recorded single-body, one-prompt, and no-batch declines
  become non-demotable `wrong_moment`; detection preserves historical ledger bytes.
- [x] Three valid batch outputs consume one capped decision; a second batch is
  blocked without another backend call. The shell documents the cap per decision.
- [x] Delegate prints the activation-off notice exactly once when unset, stays
  silent for explicit `0`/`1`, and preserves the default-off shadow gate.
- [ ] Run the three required named pytest tests on this head.
- [ ] Run their required deliberate-break/restoration controls on this head.

The new `tests/test_role_prompt_offering_cli.js` adds four real CLI cases, covering
both surfaces with classified and unclassified work. Each uses a private ledger,
checks persisted surface attribution, and repeats the consult in another process
to require byte-identical ledger state rather than duplicate match events.
Removing the CLI's `surface=args.surface` forwarding in a private source copy
produced four assertion failures; restoring its bytes produced four passes.
This additional CLI control does not replace the three required pytest controls.

```sh
node --test --test-isolation=none tests/test_role_prompt_offering_cli.js tests/test_orchestrator_loop_svg.js
python3 -m unittest discover -s tests -p test_role_prompt_offering_controls.py -v
```

The CLI suite passed four cases; the existing diagram suite passed two cases and
the receipt-parser unittest passed its six subtests. The required control script
still exits 1 at the first baseline with `No module named pytest` and no report.
PyPI installation failed because DNS resolution is unavailable; Black is also
absent. No Python files were changed or committed in this round.

The attempted PR checkbox update was rejected by automatic approval review with
`MCP tool call requires approval, but approval policy is never`. These verified
implementation checkboxes are retained here for reconciliation; the two pytest
acceptance checkboxes remain pending. PR #478 was observed open and ready for
review (`draft=false`); no merge was attempted.

The workspace's `.git` is read-only, so staging there failed before mutation.
The tested JavaScript and this record are prepared for a commit in an isolated
local checkout; they have not been pushed to the PR branch.

## Post-merge comparison disposition

Compare run `37308125678` evaluated the exact merge above and returned
CONCERNS/CONCERNS, preserved as NON_PASS. Its concrete diagram finding is valid:
the merged SVG contained conflict markers and XML parsing rejected line 3.
The follow-up removes the duplicated conflict block while retaining both batch
bindings and the value-chain description; XML parsing now passes. The first
repair removed conflict markers but retained a repeated role paragraph and lost
the value-chain sentence. The follow-up regression now checks both omissions.

Reproduce the merged-main XML failure (exit 1, line 3), then the repaired parse
(exit 0) and the two diagram regressions:

```sh
git show 4f952e3b666b04dd100ed5d698650c3bf7ead87c:orchestrator-loop.svg | python3 -c 'import sys; import xml.etree.ElementTree as ET; ET.parse(sys.stdin)'
python3 -c 'import xml.etree.ElementTree as ET; ET.parse("orchestrator-loop.svg")'
node --test --test-isolation=none tests/test_orchestrator_loop_svg.js
```

Both diagram checks pass on the repaired file. With `ORCH_TEST_LOOP_SVG`
pointing at an untouched merged-main snapshot, both checks fail; the initial
repair snapshot fails the duplicate-paragraph assertion. Removing the retained
value-chain sentence also fails the description check. XML parsing is measured
separately with ElementTree; the JavaScript checks do not implement an XML parser.

A prior keepalive environment lacked pytest and Black; its failed prerequisites
were not acceptance results. The closer independently replayed the current
767dd35 tree with Python 3.12: all three real baseline/RED/restored controls
completed, the focused suites passed 29 cases plus six subtests, whole-tree
Black checked 327 files, and both new Node diagram tests passed. The runtime
control modules remained byte-identical throughout. XML parsing also passed.
Current-head CI and the guarded review floor are separate requirements.

- [x] Merged-main SVG XML RED at line 3; repaired XML GREEN, with both descriptions retained.
- [x] Re-run all three real baseline/RED/restored pytest controls on the current source.
- [x] Re-run the 29-case/six-subtest focused suites and whole-tree Black on the current source.
- [ ] Exact-head CI, every review-thread page, expected checkout/mirror topology, and the seven-minute floor before guarded squash.
- [x] Source417 confirmed open; CONCERNS/NON_PASS preserved, with no live publication or provider PASS claimed.

Changes are limited to the diagram, this record, and JavaScript regression
tests. Production code and gates remain unchanged. Any terminal merge must use
`python3 src/merge_guard.py stranske/Orchestrator#478 --expected-head <sha> --confirm-merge`;
a blocked guard must not be bypassed.

This compact record supersedes historical interim notes that described missing
pytest/Black, uncommitted work, and ephemeral evidence paths. It supplies the
required replay commands and measured assertion/restoration outcomes directly.
The provider's truncated inspection is an evidence limitation, not a provider
PASS. Source issue 417 stays open until the bounded follow-up is gated, merged,
and its durable comparison is explicitly dispositioned. Live publication stays
manual and separate.
