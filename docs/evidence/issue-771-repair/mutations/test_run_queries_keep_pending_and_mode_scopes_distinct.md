# Mutation record: `test_run_queries_keep_pending_and_mode_scopes_distinct`

- Target: `src/feedback.py::latest_run_id_for_target` at `18ad97a054dab7f6039dd42c8131bee04267748b`
- Method: deliberate break, run single test (expect failure), restore file byte-for-byte, re-run (expect pass). Mutation removed after capture; verified by restored pass below.

## Patch

```diff
diff --git a/src/feedback.py b/src/feedback.py
index 5cdb3011..a60e3fc3 100644
--- a/src/feedback.py
+++ b/src/feedback.py
@@ -6239,6 +6239,7 @@ def runs_needing_outcome(mode: str | None = None) -> list:
 
 
 def latest_run_id_for_target(target: str, mode: str | None = None) -> str | None:
+    return None  # deliberate mutation for #1327 (reverted)
     """Return the most recent run_id recorded for a target, optionally scoped by mode."""
     with _conn() as c:
         q = "SELECT run_id FROM runs WHERE target=?"
```

## Failure output (mutation applied, exit 1)

```text
F                                                                        [100%]
=================================== FAILURES ===================================
____________ test_run_queries_keep_pending_and_mode_scopes_distinct ____________

    def test_run_queries_keep_pending_and_mode_scopes_distinct():
        feedback.record_run("local-old", "owner/repo#1", "implement", "codex", mode="local", ts=10)
        feedback.record_run("remote-new", "owner/repo#1", "implement", "claude", mode="remote", ts=20)
        feedback.record_run("other", "owner/repo#2", "review", "codex", mode="local", ts=30)
        feedback.record_outcome("local-old", durability="durable")
    
        assert feedback.runs_needing_outcome() == [
            {"run_id": "remote-new", "target": "owner/repo#1"},
            {"run_id": "other", "target": "owner/repo#2"},
        ]
        assert feedback.runs_needing_outcome("local") == [{"run_id": "other", "target": "owner/repo#2"}]
>       assert feedback.latest_run_id_for_target("owner/repo#1") == "remote-new"
E       AssertionError: assert None == 'remote-new'
E        +  where None = <function latest_run_id_for_target at 0x1056d09a0>('owner/repo#1')
E        +    where <function latest_run_id_for_target at 0x1056d09a0> = feedback.latest_run_id_for_target

tests/test_feedback_registry_queries.py:31: AssertionError
=========================== short test summary info ============================
FAILED tests/test_feedback_registry_queries.py::test_run_queries_keep_pending_and_mode_scopes_distinct
1 failed in 0.38s
/opt/anaconda3/lib/python3.12/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

```

## Restored pass output (mutation reverted, exit 0)

```text
.                                                                        [100%]
1 passed in 0.31s
/opt/anaconda3/lib/python3.12/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

```
