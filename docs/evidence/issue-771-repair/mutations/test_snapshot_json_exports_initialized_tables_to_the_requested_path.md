# Mutation record: `test_snapshot_json_exports_initialized_tables_to_the_requested_path`

- Target: `src/feedback.py::snapshot_json` at `18ad97a054dab7f6039dd42c8131bee04267748b`
- Method: deliberate break, run single test (expect failure), restore file byte-for-byte, re-run (expect pass). Mutation removed after capture; verified by restored pass below.

## Patch

```diff
diff --git a/src/feedback.py b/src/feedback.py
index 5cdb3011..45045169 100644
--- a/src/feedback.py
+++ b/src/feedback.py
@@ -6272,6 +6272,7 @@ def runs_for_target(target: str, mode: str | None = None) -> list[dict]:
 
 
 def snapshot_json(path=None) -> dict:
+    return {}  # deliberate mutation for #1327 (reverted)
     """Dump the live store to a human-readable JSON snapshot for the Code/Orchestrator project. The live
     SQLite stays on local disk (Dropbox-safe); THIS is the reviewable, version-controllable copy of the
     growing dataset. Defaults to <project>/data/feedback-snapshot.json."""
```

## Failure output (mutation applied, exit 1)

```text
eedback.record_run("snapshot", "owner/repo#3", "implement", "codex")
    
        output = tmp_path / "nested" / "snapshot.json"
        result = feedback.snapshot_json(output)
    
>       exported = json.loads(output.read_text())
                              ^^^^^^^^^^^^^^^^^^

tests/test_feedback_registry_queries.py:46: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
/opt/anaconda3/lib/python3.12/pathlib.py:1027: in read_text
    with self.open(mode='r', encoding=encoding, errors=errors) as f:
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = PosixPath('/Users/teacher/.codex/orchestrator/agent-runtime/muse/tmp/pytest-of-teacher/pytest-5/test_snapshot_json_exports_ini0/nested/snapshot.json')
mode = 'r', buffering = -1, encoding = 'locale', errors = None, newline = None

    def open(self, mode='r', buffering=-1, encoding=None,
             errors=None, newline=None):
        """
        Open the file pointed by this path and return a file object, as
        the built-in open() function does.
        """
        if "b" not in mode:
            encoding = io.text_encoding(encoding)
>       return io.open(self, mode, buffering, encoding, errors, newline)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E       FileNotFoundError: [Errno 2] No such file or directory: '/Users/teacher/.codex/orchestrator/agent-runtime/muse/tmp/pytest-of-teacher/pytest-5/test_snapshot_json_exports_ini0/nested/snapshot.json'

/opt/anaconda3/lib/python3.12/pathlib.py:1013: FileNotFoundError
=========================== short test summary info ============================
FAILED tests/test_feedback_registry_queries.py::test_snapshot_json_exports_initialized_tables_to_the_requested_path
1 failed in 0.31s
/opt/anaconda3/lib/python3.12/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

```

## Restored pass output (mutation reverted, exit 0)

```text
.                                                                        [100%]
1 passed in 0.27s
/opt/anaconda3/lib/python3.12/site-packages/pytest_asyncio/plugin.py:300: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

```
