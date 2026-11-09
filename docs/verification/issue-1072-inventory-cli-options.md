# Explicit inventory mode selection

Checked on 2026-11-09 against checkout
`e6d0c6366a03aa72b1d779e41e219ec6662198cd` plus this change.

The retained-report inventory CLI now selects modes by option presence. Previously,
an explicitly empty `--report` path could silently select Brain inventory, and
empty `--journal`, `--evidence-class` or `--evidence-reference` options bypassed
the report-mode requirement. Invalid requests now fail without emitting JSON.

Five new regressions failed before the fix and pass afterward. They exercise both
`--option ''` and `--option=` spellings against a populated Brain control, verify
unchanged input bytes, and check empty-path validation within report mode.
The retained-report, native inventory and live-cohort JavaScript suites pass all
203 tests (baseline: 198), using `node --test --test-isolation=none
--test-reporter=tap tests/test_adjudicator_inventory.js
tests/test_adjudicator_native_inventory.js tests/test_adjudicator_live_cohort.js`.
Diagnostic unittests pass 32 tests; replay/publication unittests pass seven.
JavaScript syntax and `git diff --check` pass. No Python files changed.

These are fixture checks. The configured canonical report, runtime directory and
independent outcomes database are absent. Authentic original report/journal paths
and live deployed/mirror bindings were not supplied. The first unchecked task
remains incomplete; no dispatch, canonical grading, historical proof or fixed
next-20 decision is claimed. The required retrospective pytest command with
`-m 'not slow'` cannot start because pytest is unavailable.

PR #1081 was observed open with `draft=false`. Staging fails because `.git` is
read-only; the change is preserved using isolated Git metadata and
`/tmp/orchestrator-1072-inventory-options/change.bundle`, leaving the checkout
branch and remote unchanged. Automatic approval review rejected the blocker
comment and `needs-human` label because GitHub writes require approval while
this run's approval policy is `never`; neither was published.
