# PR596 persisted receipt integrity recovery

The same-head advisory on b2eaf44db329c8b6554d0bee7d1ce6f20c4e6f0d independently reproduced two defects: a completed receipt concealed tampered canonical transcript bytes; a rejection-only row counted as a completed recheck.

The new regressions failed five assertions before the production fix (`596-red.txt`). The repair validates the canonical transcript path, refuses symlinks, and checks artifact bytes against the recorded SHA256 before completion shortcuts or partial delivery retries. Missing/corrupt evidence is an explicit error; it cannot authorize another delivery or silently replace the original execution. Reporting accepts only PASS/FAIL/UNRUNNABLE execution results and reports malformed rows separately in JSON and text.

Focused command: `python3 -m pytest -q tests/test_runtime_ac_post_merge_break.py tests/test_runtime_ac_shadow.py`. The added tests also cover a redirected transcript and malformed/unknown result values. Existing partial comment/reopen/label retry tests retain exactly-once execution and delivery. Adjacent durability tests: 86 passed, 36 subtests. Both module selftests, Black, Ruff and diff checks passed.

This is code repair evidence. The original #585 native experiment and October27 acceptance remain open; no production activation or dated acceptance is asserted.
