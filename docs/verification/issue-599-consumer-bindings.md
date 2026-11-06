# Orchestrator #599 consumer binding validation

## 2026-11-06 — closer repair (PR #1128, `cursor/issue-599`)

**Acceptance outcome (this session):** `consumer_class` returns `consumer_missing` / `ok=False` when `consumer_missing: true` **before** token resolution, so a resolvable `file.py:symbol` cannot reclassify the row to acting/grace/etc. `format_report` clarifies that DRAINABLE “declaration edit” guidance applies to prose / missing-symbol / undeclared debt, not reviewed-absence `consumer_missing` rows. Regression tests cover formatted counters and resolvable-token behavior.

**Stop rule:** at most two focused repair attempts or 20 minutes wall time before reporting; one code iteration + verification sufficed.

**Interpreter:** `/opt/anaconda3/bin/python3` (repository-selected fallback per owner policy).

### Focused pytest (required files)

```text
..............................                                           [100%]
30 passed in 98.45s (0:01:38)
EXIT:0
```

Command:

`/opt/anaconda3/bin/python3 -m pytest -q tests/test_capability_admission.py tests/test_consumer_drain_marker.py`

### Deliberate break — `test_declared_consumers_resolve_in_the_tree`

**RED** (temporarily replaced `KNOWN_GATES["route-weights-export"]["downstream_consumer"]` with prose `"the export publisher hands the document..."`; **not** a marker-only failure):

```text
AssertionError: {'KNOWN_GATES:route-weights-export': {'tokens': [], 'resolved': [], 'unresolved': []}}
assert not {'KNOWN_GATES:route-weights-export': ...
FAILED tests/test_capability_admission.py::test_declared_consumers_resolve_in_the_tree
1 failed in 1.38s
EXIT:1
```

**GREEN** (binding restored byte-for-byte to `route_weights_export.py:publish_document hands the document...`):

```text
.                                                                        [100%]
1 passed in 1.32s
EXIT:0
```

### Live post-sync counters (`ORCH_LOCAL_RUNTIME` non-empty)

Command (uses the machine’s populated runtime — **not** an empty temp dir):

`cd <repo> && ORCH_LOCAL_RUNTIME="$HOME/.codex/orchestrator" /opt/anaconda3/bin/python3 src/capability_admission.py`

Excerpt (CONSUMERS line — **DRAINABLE 1** matches **consumer_missing 1**; **prose 0**, **missing 0**):

```text
CONSUMERS (10th): 48 of 49 declare a consumer this tree can find — acting (capability-level) 9, in grace 0, not invoked 0, exercise-bound 18, unmeasured 0
                   not acting (trailing window) 21 (blocking 0, pre-cutoff debt 22); prose 0, missing 0, undeclared 0 — DRAINABLE 1 (a declaration edit each; consumer_missing fills reviewed-absence debt, not prose/missing-rail); consumer_missing 1
```

**PR / issue status:** verified live counters above are captured on this runner after the code change; broader “verified live acceptance” for the issue remains CI/hosted verification on the PR — not claimed complete here beyond these excerpts.

### Review-thread dispositions (evidence-backed)

1. **Explicit `consumer_missing` vs resolvable token:** `consumer_class` now branches on `consumer_missing is True` immediately after the undeclared check and before `consumer_resolution`; tests assert class, failed `consumer_acts`, and DRAINABLE membership with `merge_guard.py:evaluate_merge_gate` as a resolvable token.
2. **Formatted output / four counters:** `test_format_report_consumer_missing_excludes_reviewed_absence_from_declaration_edit_line` asserts `consumer_missing 1`, `prose 0`, `missing 0`, `DRAINABLE 1`, and the parenthetical disambiguation on the DRAINABLE line.
3. **Declaration prose mutation:** deliberate break on `route-weights-export` fails with `KNOWN_GATES:route-weights-export` in the assertion map; restoration passes the named test (above).

---

## Historical notes (pre-2026-11-06; may be partially stale)

The existing consumer resolver is reused. Declarations with local `file.py:symbol` rails resolve in-tree; `docs-drift-fix-agent` is the catalogue row marked `consumer_missing: true` for an external repair flow with no local acting handover. The `consumer_missing` diagnostic reaches reconciliation but does not satisfy the acting gate or infer lifecycle promotion from consumption alone.

Named checks:

- `tests/test_capability_admission.py::test_declared_consumers_resolve_in_the_tree`
- `tests/test_capability_admission.py::test_missing_consumer_declaration_survives_reconciliation_without_acceptance`

Older sections below mixed empty-runtime CLI runs with acceptance claims; **empty-runtime substitution is not used** for post-sync comparison in the 2026-11-06 evidence above.
