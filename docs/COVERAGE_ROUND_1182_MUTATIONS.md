# Coverage round 1182: executed mutations

Checked 2026-11-10 against test/source tree `e7f8daef8b9f12f2f5d5bb1900746253e4224381`.
Both named tests were executed through pytest, including their autouse temporary-runtime
and external-I/O guards. Production files were restored byte-for-byte in a `finally`
block before the restored test ran. No production mutation is committed.
These current executions replace the older pytest and standalone smoke claims.

## test_ccusage_reconcile_uses_loaded_sessions

Executed source path: `src/ccusage_reconcile.py::reconcile`. Behavior-changing mutation:

```diff
-        errors.extend(ccusage_errors)
+        errors.extend([])  # discard loader errors
```

### Mutated output

```text
command: /opt/anaconda3/bin/python3 -m pytest tests/test_ccusage_reconcile.py::test_ccusage_reconcile_uses_loaded_sessions -q --no-cov
exit: 1
F                                                                        [100%]
=================================== FAILURES ===================================
_________________ test_ccusage_reconcile_uses_loaded_sessions __________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x111d23bc0>
tmp_path = PosixPath('/Users/teacher/.codex/orchestrator/agent-runtime/codex/tmp/pytest-of-teacher/pytest-2202/test_ccusage_reconcile_uses_lo0')

    def test_ccusage_reconcile_uses_loaded_sessions(monkeypatch, tmp_path):
        calls = []

        def load_sessions(**kwargs):
            calls.append(kwargs)
            return [{"agent": "vibe", "period": "loaded-session"}], ["load err"]

        monkeypatch.setattr(ccusage, "_load_ccusage_sessions", load_sessions)
        monkeypatch.setattr(ccusage.adapters, "LEDGER", tmp_path / "ledger.ndjson")
        (tmp_path / "ledger.ndjson").write_text("")
        out = ccusage.reconcile(dry_run=True, since_days=3, strict=True)
>       assert "load err" in out["errors"]
E       AssertionError: assert 'load err' in []

tests/test_ccusage_reconcile.py:223: AssertionError
=========================== short test summary info ============================
FAILED tests/test_ccusage_reconcile.py::test_ccusage_reconcile_uses_loaded_sessions
1 failed in 0.57s
```

### Restored output

```text
command: /opt/anaconda3/bin/python3 -m pytest tests/test_ccusage_reconcile.py::test_ccusage_reconcile_uses_loaded_sessions -q --no-cov
exit: 0
.                                                                        [100%]
1 passed in 1.51s
```

## test_build_window_acquisition_blocked

Executed source path: `src/exploration_collection.py::build_window`. Behavior-changing mutation:

```diff
-        "eligible": (
-            not blocked_reasons
-            and bool(decision.get("assignments"))
-            and _exploratory_count(decision) >= min_exploratory
-        ),
+        "eligible": True,  # force ineligible result true
```

### Mutated output

```text
command: /opt/anaconda3/bin/python3 -m pytest tests/test_exploration_collection.py::test_build_window_acquisition_blocked -q --no-cov
exit: 1
F                                                                        [100%]
=================================== FAILURES ===================================
____________________ test_build_window_acquisition_blocked _____________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x1084fc5f0>

    def test_build_window_acquisition_blocked(monkeypatch):
        monkeypatch.setattr(
            eep,
            "build_plan",
            lambda **kw: _eligible_acquisition(eligible=False),
        )
        planned = []

        def plan_for_seed(**kwargs):
            planned.append(kwargs)
            return {
                "assignments": [
                    {
                        "target": "o/r#1",
                        "task_type": "implement",
                        "lane": "opener",
                        "agent": "codex",
                        "exploration": True,
                    }
                ]
            }, []

        monkeypatch.setattr(ec, "_plan_for_seed", plan_for_seed)
        item = {"target": "o/r#1", "task_type": "implement", "lane": "opener"}
        # Valid work makes acquisition eligibility the only blocker. Keep the original
        # empty-backlog case too, and verify active mode cannot bypass the same gate.
        for items in ([item], []):
            for dry_run in (True, False):
                blocked = ec.build_window(
                    backlog_payload={"items": items},
                    capacity_payload=_capacity_payload(),
                    dry_run=dry_run,
                )
                reasons = ["acquisition planner does not currently mark this mode eligible"]
                if not items:
                    reasons.append("no eligible low-risk opener backlog items selected")
                assert blocked["blocked_reasons"] == reasons
>               assert blocked["eligible"] is False
E               assert True is False

tests/test_exploration_collection.py:504: AssertionError
=========================== short test summary info ============================
FAILED tests/test_exploration_collection.py::test_build_window_acquisition_blocked
1 failed in 0.51s
```

### Restored output

```text
command: /opt/anaconda3/bin/python3 -m pytest tests/test_exploration_collection.py::test_build_window_acquisition_blocked -q --no-cov
exit: 0
.                                                                        [100%]
1 passed in 0.40s
```

## Evidence limits

The loaded-session mutation reaches the real loader-error propagation statement;
its failure is the missing `load err`, not a fixture or import failure. The restored
test also checks the loaded session, forwarded lookback, strict failure and zero writes.
The acquisition mutation changes the returned eligibility from false to true with
otherwise valid backlog work. It fails exactly at `blocked["eligible"] is False`;
it does not replace an already-true condition. The restored test covers valid/empty
backlogs in dry-run/active mode and checks zero planner calls and assignments.

These are the only two named mutation entries in this document. The remaining 21
recovered tests were checked for presence and run in focused chunks, as recorded in
[COVERAGE_ROUND_1182.md](COVERAGE_ROUND_1182.md). No mutation sensitivity is claimed
for those tests. Raw complete output is retained in
[verification/coverage-round-1182](verification/coverage-round-1182).
