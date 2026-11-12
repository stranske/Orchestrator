# Mutation record: `test_evidence_registry_normalizes_citations_and_retires_unused_rows`

- Target: `src/feedback.py::prune_dead_evidence` at `18ad97a054dab7f6039dd42c8131bee04267748b`
- Method: deliberate break, run single test (expect failure), restore file byte-for-byte, re-run (expect pass). Mutation removed after capture; verified by restored pass below.

## Patch

```diff
diff --git a/src/feedback.py b/src/feedback.py
index 5cdb3011..8cc29456 100644
--- a/src/feedback.py
+++ b/src/feedback.py
@@ -6551,6 +6551,7 @@ def bump_evidence_influence(name: str):
 
 
 def prune_dead_evidence(min_influence: int = 1) -> list[str]:
+    return []  # deliberate mutation for #1327 (reverted)
     """Evidence types never cited get retired — the dataset stays lean, not hoarded. Returns retired names."""
     with _conn() as c:
         dead = [
```

## Failure output (mutation applied, exit 1)

```text
F                                                                        [100%]
=================================== FAILURES ===================================
_____ test_evidence_registry_normalizes_citations_and_retires_unused_rows ______

    def test_evidence_registry_normalizes_citations_and_retires_unused_rows():
        assert feedback.normalize_evidence_type_citations(None) == []
        assert feedback.normalize_evidence_type_citations(
            [{"name": "  Test evidence  "}, {"type": "test   evidence"}, {}, "Other"]
        ) == ["Test evidence", "Other"]
    
        feedback.record_evidence_type("Test evidence", "verified by a focused test")
        feedback.record_evidence_type("Unused")
        assert feedback.record_evidence_type_citations(
            ["Test evidence", "missing", "test evidence"]
        ) == ["Test evidence"]
        feedback.bump_evidence_influence("Test evidence")
    
>       assert feedback.prune_dead_evidence() == ["Unused"]
E       AssertionError: assert [] == ['Unused']
E         
E         Right contains one more item: 'Unused'
E         Use -v to get more diff

tests/test_feedback_registry_queries.py:66: AssertionError
=========================== short test summary info ============================
FAILED tests/test_feedback_registry_queries.py::test_evidence_registry_normalizes_citations_and_retires_unused_rows
1 failed in 0.28s
/opt/anaconda3/lib/python3.12/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

```

## Restored pass output (mutation reverted, exit 0)

```text
.                                                                        [100%]
1 passed in 0.25s
/opt/anaconda3/lib/python3.12/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

```
