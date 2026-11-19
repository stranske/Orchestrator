# Mutation record: `test_evidence_proposals_and_approval_require_recurrence_then_reactivate`

- Target: `src/feedback.py::approve_evidence_type` at `18ad97a054dab7f6039dd42c8131bee04267748b`
- Method: deliberate break, run single test (expect failure), restore file byte-for-byte, re-run (expect pass). Mutation removed after capture; verified by restored pass below.

## Patch

```diff
diff --git a/src/feedback.py b/src/feedback.py
index 5cdb3011..5a67a323 100644
--- a/src/feedback.py
+++ b/src/feedback.py
@@ -6492,7 +6492,7 @@ def approve_evidence_type(
             "SELECT COUNT(*) FROM evidence_gaps WHERE gap=? AND status='open' AND ts>=?",
             (gap, since),
         ).fetchone()[0]
-        eligible = recurrence >= min_recurrence
+        eligible = True
         existing = c.execute(
             "SELECT status, rationale FROM evidence_types WHERE name=?", (name,)
         ).fetchone()
```

## Failure output (mutation applied, exit 1)

```text
F                                                                        [100%]
=================================== FAILURES ===================================
___ test_evidence_proposals_and_approval_require_recurrence_then_reactivate ____

    def test_evidence_proposals_and_approval_require_recurrence_then_reactivate():
        with pytest.raises(ValueError, match="name"):
            feedback.approve_evidence_type("", "gap")
        with pytest.raises(ValueError, match="gap"):
            feedback.approve_evidence_type("Evidence", "")
    
        feedback.record_evidence_gap("pr-1", "reviewer", "missing provenance")
        preview = feedback.approve_evidence_type("Provenance", "missing provenance", min_recurrence=2)
>       assert preview["eligible"] is False
E       assert True is False

tests/test_feedback_registry_queries.py:85: AssertionError
=========================== short test summary info ============================
FAILED tests/test_feedback_registry_queries.py::test_evidence_proposals_and_approval_require_recurrence_then_reactivate
1 failed in 0.28s
/opt/anaconda3/lib/python3.12/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

```

## Restored pass output (mutation reverted, exit 0)

```text
.                                                                        [100%]
1 passed in 0.28s
/opt/anaconda3/lib/python3.12/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

```
