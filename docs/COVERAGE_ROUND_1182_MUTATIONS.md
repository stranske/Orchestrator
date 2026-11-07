# Coverage round 1182: executed mutations

Check date: 2026-11-07. Recovery source: `515371bd806b1b297704b3fef796274a216e6bde`.
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

tests/test_ccusage_reconcile.py:217: AssertionError
=========================== short test summary info ============================
FAILED tests/test_ccusage_reconcile.py::test_ccusage_reconcile_uses_loaded_sessions
1 failed in 0.52s
```

Restored output (exit 0):

```text
.                                                                        [100%]
1 passed in 0.47s
```


## test_build_window_acquisition_blocked

Production path: `src/exploration_collection.py`. Mutation:

```diff
-        blocked_reasons.append("acquisition planner does not currently mark this mode eligible")
+        blocked_reasons.append("mutated: acquisition allowed")
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

tests/test_exploration_collection.py:476: AssertionError
=========================== short test summary info ============================
FAILED tests/test_exploration_collection.py::test_build_window_acquisition_blocked
1 failed in 0.66s
```

Restored output (exit 0):

```text
.                                                                        [100%]
1 passed in 0.49s
```
