# Issue #778 Validation Evidence

Durable validation report for finishing stranske/Orchestrator#778 (closer repair). Implementation
tests landed in merged PR #779 (`707e4f722043b2c907024ab7546911999f983f65`); this document records
break-and-revert proof, CI pytest authority, and Gate combined-coverage deltas without changing
production code.

## Criteria Status

- [x] Recover or reproduce a named mutation failure and restored pass for each of the five new tests
- [x] Record a passing CI pytest result and authoritative coverage delta (branches noted where measured)
- [x] Document the allowed shortfall, target-selection ranking, and available pre-work coverage evidence

## Recovered PR #779 Context

| Field | Value |
| --- | --- |
| PR | https://github.com/stranske/Orchestrator/pull/779 |
| Merge commit | `707e4f722043b2c907024ab7546911999f983f65` |
| Files | `tests/test_repo_knowledge_rule_lifecycle.py`, `tests/test_tick_helpers_coverage.py` |
| Authoritative CI (green) | https://github.com/stranske/Orchestrator/actions/runs/39223779522 (`headSha` `9c0bbd809d2406af03ac1cdf4f055980d00c3126`) |
| Post-merge combined coverage job | https://github.com/stranske/Orchestrator/actions/runs/39229590060 (coverage job success; verify failed only on collection floor lag at merge) |

PR #779 body (summary): test-only hermetic coverage for ranked targets `src/tick.py` and
`src/repo_knowledge.py`; local `pytest --cov` deltas +24 / +90 statements; ~114 statement delta vs
~180 combined-unit goal; opener did not finish a local `verify.py --coverage` run in-session.

## 1. Pytest Runs (named command)

### Local (closer repair worktree, 2026-11-03)

```bash
/opt/anaconda3/bin/python3 -m pytest -q \
  tests/test_repo_knowledge_rule_lifecycle.py \
  tests/test_tick_helpers_coverage.py
```

```
.....                                                                    [100%]
5 passed in 0.97s
```

Verbose node IDs:

```
tests/test_repo_knowledge_rule_lifecycle.py::test_current_refs_reject_unsafe_and_stale_references PASSED
tests/test_repo_knowledge_rule_lifecycle.py::test_managed_rule_and_bundle_apply_preview_duplicate_and_removal PASSED
tests/test_tick_helpers_coverage.py::test_target_helpers_reject_malformed_targets_and_split_valid_targets PASSED
tests/test_tick_helpers_coverage.py::test_research_feed_reports_each_invalid_and_fresh_state PASSED
tests/test_tick_helpers_coverage.py::test_public_plan_metadata_and_order_helpers_preserve_only_safe_fields PASSED
```

### CI (authority for merged tests)

From run `39223779522` job **verify.py (real pytest counts + selftests + capability gates)**:

```
pytest:     5655 passed, 0 failed, 27/31 max skipped (5682 collected; floor 5677 — 5 BEHIND)
```

The five tests above are included in that aggregate; the run conclusion is **success**
(https://github.com/stranske/Orchestrator/actions/runs/39223779522).

## 2. Deliberate-Break / Restore (2026-11-03, not committed)

Each cycle: patch production source → `pytest -q <nodeid>` (expect fail) → restore source →
`pytest -q <nodeid>` (expect pass). Mutations match PR #779’s described intent.

### 1. `test_target_helpers_reject_malformed_targets_and_split_valid_targets`

- **Module**: `src/tick.py`, `_target_repo`
- **Mutation**: `return target.split("#", 1)[0]` → `return None`
- **Failure**: `AssertionError: assert None == 'owner/repo'` at `tests/test_tick_helpers_coverage.py:45`
- **Restored pass**: 1 passed

### 2. `test_research_feed_reports_each_invalid_and_fresh_state`

- **Module**: `src/tick.py`, `load_research_feed`
- **Mutation**: `FileNotFoundError` branch returns `{"status": "fresh", "items": []}` instead of `absent`
- **Failure**: `AssertionError` on first assert (`fresh` vs `absent`) at `tests/test_tick_helpers_coverage.py:54`
- **Restored pass**: 1 passed

### 3. `test_public_plan_metadata_and_order_helpers_preserve_only_safe_fields`

- **Module**: `src/tick.py`, `_public_research_plan` / `clean_job`
- **Mutation**: `return {k: v for k, v in job.items() if k != "item"}` → `return {}`
- **Failure**: `AssertionError` on candidates shape at `tests/test_tick_helpers_coverage.py:88`
- **Restored pass**: 1 passed

### 4. `test_current_refs_reject_unsafe_and_stale_references`

- **Module**: `src/repo_knowledge.py`, `validate_current_refs`
- **Mutation**: `"valid": not errors and len(normalized) == len(refs),` → `"valid": True,`
- **Failure**: `assert True is False` at `tests/test_repo_knowledge_rule_lifecycle.py:66`
- **Restored pass**: 1 passed

### 5. `test_managed_rule_and_bundle_apply_preview_duplicate_and_removal`

- **Module**: `src/repo_knowledge.py`, `install_managed_rule`
- **Mutation**: `if duplicate or not apply:` → `if True:` (always preview-only return)
- **Failure**: `assert False is True` on `applied["applied"]` at `tests/test_repo_knowledge_rule_lifecycle.py:87`
- **Restored pass**: 1 passed

**Summary**: 5/5 mutations failed as expected; all five pass after restoration.

## 3. Gate / Combined CI Coverage (authoritative)

**Command (repository Gate / CI):** `python3 src/verify.py --aggregate shard-evidence --coverage`
(with `ORCH_STATE_DIR` and `ORCH_LOCAL_RUNTIME` on the runner temp dirs), as in
`.github/workflows/ci.yml` job **verify.py aggregate + combined coverage**.

**Artifacts downloaded (GitHub Actions):**

| Role | Run ID | Artifact | Timestamp (UTC) |
| --- | ---: | --- | --- |
| Pre–PR #779 baseline (last stored combined snapshot with issue-ranked missing counts) | 39224318238 | `gate-coverage/coverage.json` | 2026-10-19T08:45:29 |
| PR #779 head / post-merge equivalent | 39223779522 / 39229590060 | `gate-coverage/coverage.json` | 2026-10-19T08:40:21 / 09:28:10 |

`coverage.json` meta for these payloads: `format` 3, `branch_coverage`: **false** (statements/lines
only in the artifact; blended line+branch **percent** in Gate text uses the fleet trend producer,
but per-file branch counts are not present in these JSON files).

### Whole tree

| Metric | Pre baseline | After #779 (CI artifact) | Delta |
| --- | ---: | ---: | ---: |
| Combined percent (statements) | 86.7% | 86.7% | ~0.0 pp |
| Covered statements (totals) | 65,274 | 65,289 | **+15** |

Trend artifact (`gate-coverage-trend/coverage-trend.json`, run `39229590060`): `current` 86.67870371599645.

### Targets (issue #778 ranking)

Issue body ranked targets (combined pytest+selftest **statements+branches** at dispatch):

1. `src/tick.py` — **30** uncovered units (4 escaped-defect signals, churn 9)
2. `src/repo_knowledge.py` — **173** uncovered units (4 escaped-defect signals, churn 8)

CI `coverage.json` **missing line** counts (comparable pre/post artifact pair):

| File | Pre missing lines | Post missing lines | Δ missing |
| --- | ---: | ---: | ---: |
| `src/tick.py` | 30 | 26 | **−4** |
| `src/repo_knowledge.py` | 173 | 162 | **−11** |
| **Sum (two targets)** | 203 | 188 | **−15** |

Post-merge authoritative per-file coverage (run `39229590060`):

- `src/tick.py`: 557/583 statements covered (95.5%), 26 missing lines
- `src/repo_knowledge.py`: 1173/1335 statements covered (87.9%), 162 missing lines

### Allowed shortfall vs ~180-unit chunk goal

- **Goal**: ~180 combined uncovered **statements+branches** in one chunk (issue #778 scope text).
- **Measured CI delta (artifact pair above)**: **15** fewer missing **statement lines** on the two
  targets (4 + 11), whole-tree **+15** covered statements.
- **PR #779 local module `pytest --cov` (not CI combined)**: +114 statements (~66 short of 180) —
  opener documented in PR #779; not reproduced here as authoritative.
- **Why partial is expected**: Hermetic tests exercise pure helpers and registry lifecycle paths;
  most remaining `tick.py` / `repo_knowledge.py` mass requires live `gh`, backlog feeds, compiler
  paths, or selftest-only edges. No production defect was found; expanding scope would chase metric
  units without new discriminating behavior (PR #779 conclusion).

## 4. Ranking and Pre-Work Evidence

### Recovered ranking (issue #778 body, 2026-10-19 dispatch)

Order: escaped-defect signal → churn → uncovered mass; bundled ~180 units per PR.

1. `src/tick.py` (30 units at dispatch)
2. `src/repo_knowledge.py` (173 units at dispatch)

Reserved / in-flight files listed in the issue were not touched (see issue body).

### Pre-work measurements recovered

| Source | What it contains |
| --- | --- |
| Issue #778 body | Rank order, 30 + 173 pre-chunk uncovered units, ~180 bundle target, 86.66% fleet blended context |
| PR #779 body | Break-and-revert descriptions, local `pytest --cov` before/after table, shortfall vs 180 |
| CI artifact run `39224318238` | Pre snapshot with `tick.py` missing 30 / `repo_knowledge.py` missing 173 lines |
| CI artifact run `39223779522` / `39229590060` | Post snapshot with 26 / 162 missing lines |

### Historical evidence **not** recoverable on this machine

- Opener’s **in-session** `python3 src/verify.py --coverage` combined run (PR #779: timed out /
  not completed — no log or `coverage.json` retained).
- Machine-local ranking JSON behind dispatch (issue text is the only preserved ranking for #778).
- Per-branch combined **branch** counts for the two targets in `coverage.json` (artifacts declare
  `branch_coverage: false`).
- CI run for merge parent `93d96f74e2f1ed586b18c53852350a480bd65bb8` (floor-only push): **no**
  `ci.yml` combined-coverage artifact; baseline above uses the last prior combined snapshot whose
  per-file missing counts match the issue’s ranked 30/173.

## Artifact file references (local closer session)

Downloaded for inspection (not committed; regenerate with `gh run download`):

- `/tmp/orch778-pre779-cov/coverage.json` ← run `39224318238`
- `/tmp/orch778-pr779-cov/coverage.json` ← run `39223779522`
- `/tmp/orch778-cov/coverage.json` ← run `39229590060`
- `/tmp/orch778-trend/coverage-trend.json` ← run `39229590060`
