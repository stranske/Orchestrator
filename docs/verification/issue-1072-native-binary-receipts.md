# Native receipt storage diagnostics

Checked on 2026-11-08 against checkout
`7d2ea69bbb8d4b26c7b0fd5ce2066d6f10913a24` plus this change.

The first unchecked task requires an inventory of existing native receipts and
paid attempts before dispatch. SQLite permits binary values in TEXT columns.
The retained-report inventory previously sent a binary raw receipt through the
ordinary validation recovery path despite being unable to hash it as text.
It now records `raw_response_invalid_type` and requests evidence recovery without
redispatch. Binary validation storage has its own `invalid_storage_type`
diagnosis before JSON parsing. Receipt presence remains separate from usability.

Three regressions cover raw-persisted and validated raw receipts, and validated
result storage. Each exercises valid UTF-8 bytes, empty bytes and invalid UTF-8
through both the API and CLI. Malformed receipts remain in case recovery and
retained-attempt locators. Paid identities, unresolved in-flight attempts,
original decisions and UNKNOWN truth remain visible. Inventory preserves report,
journal and manifest bytes and publishes no private payloads. Restoring text
permits the existing native authentication path without granting grade authority.
All three tests failed before the implementation change and pass afterward.

Validation:

- `node --test --test-isolation=none --test-reporter=tap tests/test_adjudicator_inventory.js tests/test_adjudicator_native_inventory.js tests/test_adjudicator_live_cohort.js`: 190 passed.
- Diagnostic unittests: 32 passed; replay/publication unittests: seven passed.
- JavaScript syntax and `git diff --check`: passed.
- `python3 -m pytest tests/test_adjudicator_retro.py -m 'not slow'`: cannot start
  because pytest is unavailable. Installing pytest and Black failed. No Python
  files changed.

These are isolated fixture results. The configured runtime, canonical report,
independent outcomes database and default mirror are absent in this runner.
Authentic original/journal locations and deployment/mirror bindings remain
unavailable. No production inventory, dispatch, canonical refresh, historical
observation or live-shadow decision is claimed. No operational task checkbox is
complete.

Normal staging failed because `.git/index.lock` is on a read-only filesystem.
The verified change is preserved using isolated Git metadata and
`/tmp/orchestrator-1072-binary-receipts/change.bundle`; the checkout branch and
remote are unchanged.
