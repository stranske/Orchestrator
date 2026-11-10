# Coverage round 1182: executed mutations

Check date: 2026-11-10. Recovery source: `515371bd806b1b297704b3fef796274a216e6bde`.
Historical mutation claims are not carried forward as evidence. The two executions below are new, actual break-and-restore runs.


## test_ccusage_reconcile_uses_loaded_sessions

Production path: `src/ccusage_reconcile.py`. Mutation:

```diff
-        errors.extend(ccusage_errors)
+        errors.extend([])  # temporary mutation: drop loader errors
```

Command (both runs):

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_ccusage_reconcile.py::test_ccusage_reconcile_uses_loaded_sessions -q --no-cov
```

Mutated output (exit 1):

```text
F                                                                        [100%]
=================================== FAILURES ===================================
_________________ test_ccusage_reconcile_uses_loaded_sessions __________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x109529e20>
tmp_path = PosixPath('/Users/teacher/.codex/orchestrator/agent-runtime/codex/tmp/pytest-of-teacher/pytest-1643/test_ccusage_reconcile_uses_lo0')

    def test_ccusage_reconcile_uses_loaded_sessions(monkeypatch, tmp_path):
        monkeypatch.setattr(ccusage, "_load_ccusage_sessions", lambda **kw: ([], ["load err"]))
        monkeypatch.setattr(ccusage.adapters, "LEDGER", tmp_path / "ledger.ndjson")
        (tmp_path / "ledger.ndjson").write_text("")
        out = ccusage.reconcile(dry_run=True)
>       assert "load err" in out["errors"]
E       AssertionError: assert 'load err' in []

tests/test_ccusage_reconcile.py:214: AssertionError
=========================== short test summary info ============================
FAILED tests/test_ccusage_reconcile.py::test_ccusage_reconcile_uses_loaded_sessions
1 failed in 0.43s
```

Restored output (exit 0):

```text
.                                                                        [100%]
1 passed in 0.41s
```


## test_build_window_acquisition_blocked

Production path: `src/exploration_collection.py`. Mutation:

```diff
-    if not window.get("eligible"):
+    if False:  # temporary mutation: suppress the ineligible-window result
```

Command (both runs):

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_exploration_collection.py::test_build_window_acquisition_blocked -q --no-cov
```

Mutated output (exit 1):

```text
F                                                                        [100%]
=================================== FAILURES ===================================
____________________ test_build_window_acquisition_blocked _____________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x1062d8c20>

    def test_build_window_acquisition_blocked(monkeypatch):
        monkeypatch.setattr(
            eep,
            "build_plan",
            lambda **kw: _eligible_acquisition(eligible=False),
        )
        blocked = ec.build_window(
            backlog_payload={"items": []},
            capacity_payload=_capacity_payload(),
        )
>       assert any("does not currently mark" in r for r in blocked["blocked_reasons"])
E       assert False
E        +  where False = any(<generator object test_build_window_acquisition_blocked.<locals>.<genexpr> at 0x106380790>)

tests/test_exploration_collection.py:473: AssertionError
=========================== short test summary info ============================
FAILED tests/test_exploration_collection.py::test_build_window_acquisition_blocked
1 failed in 0.53s
```

Restored output (exit 0):

```text
.                                                                        [100%]
1 passed in 0.49s
```

## Current acquisition test strengthening: smoke evidence only

The current test additionally checks the ineligible result and verifies no planning
occurs with otherwise valid work, in both dry-run and active mode. It retains the
empty-backlog assertions above. The current runner cannot execute the named pytest
test because pytest is absent; the historical pytest evidence above is retained
with its original command and output. No new pytest pass is claimed here.

A separate standard-library smoke script used the same four input cases and
assertions against `build_window`, temporary runtime roots, forbidden external
process/network calls, and a planner stub returning an exploratory assignment.
Command: `python3 /tmp/orch_1182_acquisition_smoke.py`. This temporary smoke script
does not import or execute `test_build_window_acquisition_blocked`.

First mutation:

```diff
-    if not window.get("eligible"):
+    if False:  # temporary mutation: suppress acquisition blocker
```

Actual smoke failure (exit 1):

```text
  File "/tmp/orch_1182_acquisition_smoke.py", line 64, in <module>
    assert blocked["blocked_reasons"] == reasons, blocked["blocked_reasons"]
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: []
```

Second mutation changes the returned eligibility result, independently of the
blocking-reason condition:

```diff
-        "eligible": (
-            not blocked_reasons
-            and bool(decision.get("assignments"))
-            and _exploratory_count(decision) >= min_exploratory
-        ),
+        "eligible": True,  # temporary mutation: force an ineligible result true
```

Actual smoke failure (exit 1):

```text
  File "/tmp/orch_1182_acquisition_smoke.py", line 65, in <module>
    assert blocked["eligible"] is False, blocked["eligible"]
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: True
```

After **each** mutation, the production source was restored byte-for-byte and the
smoke command exited 0:

```text
Acquisition smoke: 4 cases passed; no planning or external I/O
```

The required named pytest mutation run still needs an environment with pytest;
these smoke results do not complete that task or replace its required evidence.
