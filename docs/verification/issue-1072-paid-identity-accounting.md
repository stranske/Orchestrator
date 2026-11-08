# Native paid-identity accounting

Checked on 2026-11-08 against checkout
`c42cca1e0f535e21216e8fb1792f3714b98a52fc` plus this change.

The retained-report exporter now requires nonblank text when counting native
provider identities. Null, empty, whitespace-only and non-text values remain
visible in receipt locators but cannot increase the unique paid-identity count
or hide in-flight attempts from `unresolved_without_backend_id`. Valid IDs retain
their exact text; checking presence does not trim or merge provider identities.

The new API/CLI regression fails against the previous exporter: whitespace adds
a false paid identity and removes both in-flight recovery obligations. Both new
tests pass after the fix. All 184 tests across the retained-report, native
inventory and live-cohort JavaScript suites pass. Diagnostic and replay/publication
unittests pass 32 and seven tests. JavaScript syntax and whitespace checks pass.
Fixtures preserve report/journal bytes, saved paid history, UNKNOWN later truth,
receipt locators and the authentication/dispatch boundary.

These are fixture results. The configured production runtime, canonical report
and outcomes database are absent. Authentic original/journal locations and live
deployment/mirror bindings remain unavailable. No operational checkbox is complete;
no dispatch, publication, production grade or live decision is claimed.
The required retrospective pytest command with `-m 'not slow'` cannot start:
pytest is absent and package installation failed. No Python files changed.

Normal staging fails because Git metadata is read-only. The focused change is
preserved with isolated Git metadata and a transferable bundle at
`/tmp/orchestrator-1072-paid-identities/change.bundle`; the checkout branch and
remote are unchanged.
