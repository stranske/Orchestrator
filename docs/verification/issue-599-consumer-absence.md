# Source 599: reviewed consumer absence

Checked 2026-11-08 America/Chicago (2026-11-09 UTC), from main
`69d32fa9a5ca3a8058d37ba9ce75bf590a6495d5`.

The 2026-11-06 owner clarification makes the named declaration test the gate:
`consumer_missing: true` is missing-consumer debt, not accidental prose. The
report's `missing` count includes those rows; DRAINABLE retains them. Missing
consumers still fail `req_consumer_acts`, retain their existing enforcement and
grace rules, and receive no acting, lifecycle or acceptance credit.

The real local ledger `/Users/teacher/.codex/orchestrator/capabilities.json`
contains 49 live rows. Before this change its only prose row was
`docs-drift-fix-agent`, which was already explicitly marked `consumer_missing`.
The focused consumer projection after this change prints:

```text
CONSUMERS (10th): 48 of 49 declare a consumer this tree can find
prose 0, missing 1, undeclared 0 — DRAINABLE 1
(reviewed absent 1, unresolved 0; reviewed absence remains finding debt)
```

This is a read-only declaration projection, not a full admission verdict,
acting measurement, mirror deployment, or hosted acceptance claim. The full
`python3 src/capability_admission.py` invocation on the canonical checkout
produced no stdout within two minutes and was stopped. Its unrelated audit and
commitment checks were not changed. Reproduce the focused live metric in under
two minutes from this checkout:

```bash
PYTHONPATH=src /opt/anaconda3/bin/python3 - <<'PY'
import capabilities
import capability_admission as admission

ledger = capabilities.load_declared(capabilities.REG)  # reconciled copy; no write
live = {cid: cap for cid, cap in ledger.items()
        if cap.get("status") not in capabilities.NOT_LIVE_STATES}
classes = {name: [] for name in admission.CONSUMER_CLASSES}
for cid, cap in live.items():
    classes[admission.consumer_class(cap, {})[0]].append(cid)
prose = len(classes["prose"])
missing = len(classes["consumer_missing"]) + len(classes["unresolved"])
undeclared = len(classes["undeclared"])
drainable = prose + missing + undeclared
print(f"CONSUMERS: {len(live)} live; prose {prose}, missing {missing}, "
      f"undeclared {undeclared} — DRAINABLE {drainable}")
print("reviewed absent:", classes["consumer_missing"])
PY
```

## Behavior evidence

- New behavior tests against unchanged main: two intended assertion failures,
  one pass, in 56.83 seconds. Reviewed absence was classified as prose.
- After the fix:
  `/opt/anaconda3/bin/python3 -m pytest tests/test_consumer_missing_classification.py tests/test_capability_admission.py -k 'consumer or format_report' -q`
  produced 14 passes, 16 deselections, in 35.15 seconds.
- Deliberate break: temporarily replace the real
  `route_weights_export.py:publish_document` declaration with
  `the export publisher`. The named
  `test_declared_consumers_resolve_in_the_tree` fails with
  `KNOWN_GATES:route-weights-export`, exit 1, in 25.83 seconds. Restore the exact
  source bytes before the final focused run.
- Existing admission tests still assert the before/after-cutoff blocking and
  deferred dispositions, unchanged lifecycle/history/outcome links, and no
  acting credit. A resolvable token cannot override explicit reviewed absence.
- Focused Ruff and Black checks and `git diff --check` pass.

The implementation preserves the valid classification and regression approach
from closed PR 1128. It omits that branch's artificial
`CONSUMER_DRAIN_MARKER`, uses the clarified `missing` count, and keeps the
already merged source declarations from PR 633. Neither historical branch is
deleted or superseded by a closure in this round. Raw local transcripts and
native ownership evidence are retained under
`~/.codex/automations/pd-workloop-resume/runs/20261109T0010Z/`.

## Same-branch review and CI recovery

Post-push CI exposed an unassigned test module in
`test_shard_lists_partition_every_collected_test_exactly_once`. Register the new
module in shard 4, beside the admission tests, and list it as untimed; do not
invent timing measurements or change verification floors.

CodeRabbit thread `discussion_r4508465504` identified a real forward-transition
gap: the old `consumer_missing` flag survived a declaration that replaces the
consumer and omits the flag. Reconciliation now removes that stale diagnostic
only when the declaration owns `downstream_consumer`; explicit True and False
values remain authoritative. The ordinary reconciliation event records the
field removal, and a second reconciliation is idempotent. Lifecycle and prior
invocation/outcome evidence remain unchanged.

The new transition test failed on `consumer_missing` still being present before
the fix (one intended failure, one pass). After repair, the focused command above
produced 17 passes, 16 deselections, in 16.74 seconds. The three added controls
cover clearing a replaced consumer, explicit flag values, and declarations that
do not own the consumer. The broader module selftest was bounded at 115 seconds
and timed out without output; it is not counted as a pass.

## Continuation round: acceptance target and supported report (2026-11-08)

Recorded before edits, 2026-11-08 22:50 America/Chicago (2026-11-09 04:50 UTC).
Outcome: one supported `capability_admission.py` invocation, run against the
populated runtime, prints the formatted CONSUMERS counters and the named
`consumer_missing` rows in under 120 seconds. Stop rule: two attempts or 20
minutes.

### Acceptance target

The issue body carries the clarification verbatim under "Owner clarification
(2026-11-06)". It is marked "Decided under the owner's delegation during the
needs-human catalog review". So it is a delegated decision recorded on the issue,
not a statement Tim made himself. It reads: "The named test is the gate.
Entries marked `consumer_missing: true` are allowed and are not prose. The pass
number is `prose 0` on the live ledger, with `missing` equal to the count of
`consumer_missing` rows (that count is DRAINABLE)."

Resulting target: `prose 0`, `missing` == count of `consumer_missing` rows ==
DRAINABLE, on the live ledger after sync. The original target (`prose 0,
missing 0` by 2026-10-27) is superseded by that clarification and was not met.
The counter semantics were not changed to print zero.

### Supported invocation and real output (branch evidence)

Source: branch `codex/issue-599-consumer-absence` at
`c5533916d203de532239554f0e40e580ef295a98` (origin/main at the time:
`2f00addbc3420ca67f50399d6585c19a09ec8220`). Ledger:
`/Users/teacher/.codex/orchestrator/capabilities.json`, sha256
`20a20c0dcbce48ee18f56fc1f0e3fc35321725f6ef414e1e2a720d7df077e8e0`. Python
3.12.2 (`/opt/anaconda3/bin/python3`).

The timeout recorded earlier did not reproduce. Attempt 1 of 2 completed in 18
seconds with exit 0, so no new entry point was added:

```bash
ORCH_LOCAL_RUNTIME="$HOME/.codex/orchestrator" timeout 120 /opt/anaconda3/bin/python3 src/capability_admission.py
```

```text
  CONSUMERS (10th): 49 of 50 declare a consumer this tree can find — acting (capability-level) 9, in grace 0, not invoked 0, exercise-bound 19, unmeasured 0
                   not acting (trailing window) 21 (blocking 0, pre-cutoff debt 22); prose 0, missing 1, undeclared 0 — DRAINABLE 1 (reviewed absent 1, unresolved 0; reviewed absence remains finding debt)
```

The existing `--json` flag names the inventory (exit 0, 18 seconds):

```bash
ORCH_LOCAL_RUNTIME="$HOME/.codex/orchestrator" timeout 120 /opt/anaconda3/bin/python3 src/capability_admission.py --json
```

```text
consumers.by_class.prose            []
consumers.by_class.consumer_missing ["docs-drift-fix-agent"]
consumers.drainable                 ["docs-drift-fix-agent"]
```

On the branch the clarified target holds: prose 0, missing 1, one
`consumer_missing` row, DRAINABLE 1, and the DRAINABLE set is exactly the
`consumer_missing` set. The same report lists one enforced failure
(`local-loop`, admission parts). That failure is unrelated to this issue and was
not changed.

Focused tests on the same head:
`/opt/anaconda3/bin/python3 -m pytest tests/test_consumer_missing_classification.py tests/test_capability_admission.py -k 'consumer or format_report' -q`
printed `17 passed, 16 deselected in 3.28s`.

### Post-sync delivery: PENDING

Continuation checked 2026-11-09 07:48 UTC (2026-11-09 America/Chicago),
against PR #1275 head `ae9e83875d8ac6837d19305e6f90757e8678d333`.
The acceptance outcome remains a deployed report with prose, unresolved and
undeclared sets empty, and DRAINABLE exactly equal to `consumer_missing`.
The commands below remain the acceptance commands; the execution stop rule is
two attempts or 20 minutes. No deployed acceptance attempt was made because
the prerequisite CI gate is failing.

Authority was re-read with
`gh issue view 599 --repo stranske/Orchestrator --json body,comments`.
The issue body records the 2026-11-06 clarification and explicitly attributes it
to the owner's delegation during the needs-human catalog review. This confirms
the recorded delegated target, not an independently observed delegation
conversation: missing equals the reviewed-absence count, rather than zero.
The comments retain the earlier park and subsequent restart; they do not supply
a newer counter target. Authority: <https://github.com/stranske/Orchestrator/issues/599>.

`git worktree list --porcelain` identifies this checkout as the sole registered
holder of `codex/issue-599-consumer-absence`; no preserved ref naming #1275 or
that branch was found. Existing holders and implementation were left intact.

The required focused command was run unchanged:

```bash
/opt/anaconda3/bin/python3 -m pytest tests/test_consumer_missing_classification.py tests/test_capability_admission.py -k 'consumer or format_report' -q
```

```text
.................                                                        [100%]
17 passed, 16 deselected in 3.01s
```

First broken delivery step: Gate run `42574862530`, shard 2/6 on both Python
3.13 and 3.14, failed before running tests. Inspection command:
`gh run view 42574862530 --repo stranske/Orchestrator --log-failed`.
Exact terminal outputs:

```text
4 workers [0 items]
============================ no tests ran in 0.89s =============================
Process completed with exit code 5.
```

The 0.89s output is Python 3.14; Python 3.13 printed `no tests ran in 0.79s`
and the same exit code. Worker diagnostics report exitstatus 4; the underlying
collection cause is unknown from these logs. The Gate summary reports
`Gate reported failure:  Python CI result: failure.`
Run: <https://github.com/stranske/Orchestrator/actions/runs/42574862530>.

Blocking/drainable pair: failing CI blocks merge and therefore post-sync
acceptance; issue authority, focused tests, and this evidence update remain
drainable while that gate is closed. No merge, sync, pin edit, or CI bypass was
performed. Deployed commit, generation, and post-sync counters remain UNKNOWN;
the PENDING status cannot truthfully be replaced with deployed success evidence.

The evidence above comes from the branch code reading the live ledger. The
launchd mirror still runs main, so this is not post-sync acceptance. After the
owning delivery lane merges and syncs, it must rerun the two commands above from
`~/.codex/orchestrator-mirror`, record the deployed generation and commit, and
confirm `prose 0` with DRAINABLE equal to the `consumer_missing` count. Until
then the acceptance target is unmet.

2026-11-09: Gate run https://github.com/stranske/Orchestrator/actions/runs/42590538518 (head 0dc9a8a1bd7e0259c7533a9bd45753729c3b0413) passed — every Python CI shard collected tests on Python 3.13 and 3.14.
