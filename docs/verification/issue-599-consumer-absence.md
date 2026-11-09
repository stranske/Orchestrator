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
