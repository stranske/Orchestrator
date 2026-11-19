# Mutation record: `test_role_selector_metrics_preserve_no_match_and_rejection_signals`

- Target: `src/feedback.py::record_role_selector_event` at `18ad97a054dab7f6039dd42c8131bee04267748b`
- Method: deliberate break, run single test (expect failure), restore file byte-for-byte, re-run (expect pass). Mutation removed after capture; verified by restored pass below.

## Patch

```diff
diff --git a/src/feedback.py b/src/feedback.py
index 5cdb3011..a24325d6 100644
--- a/src/feedback.py
+++ b/src/feedback.py
@@ -4505,6 +4505,7 @@ def record_role_selector_event(
     disagreement: bool = False,
     role_run_id: str | None = None,
 ) -> dict:
+    return  # deliberate mutation for #1327 (reverted)
     """Record a bounded role-selector decision in the existing completion-event plane.
 
     Selector misses are learning evidence too: ``no_matching_work`` is intentionally
```

## Failure output (mutation applied, exit 1)

```text
F                                                                        [100%]
=================================== FAILURES ===================================
______ test_role_selector_metrics_preserve_no_match_and_rejection_signals ______

    def test_role_selector_metrics_preserve_no_match_and_rejection_signals():
>       with pytest.raises(ValueError, match="role_name"):
E       Failed: DID NOT RAISE ValueError

tests/test_feedback_registry_queries.py:122: Failed
=========================== short test summary info ============================
FAILED tests/test_feedback_registry_queries.py::test_role_selector_metrics_preserve_no_match_and_rejection_signals
1 failed in 0.26s
/opt/anaconda3/lib/python3.12/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

```

## Restored pass output (mutation reverted, exit 0)

```text
.                                                                        [100%]
1 passed in 0.29s
/opt/anaconda3/lib/python3.12/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

```
