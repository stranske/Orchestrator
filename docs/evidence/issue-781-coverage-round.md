# Issue #781 — coverage round acceptance (closer repair of merged PR #782)

Durable acceptance record for stranske/Orchestrator#781. Implementation tests landed in merged
PR #782 (`2ea9b541d16e70df49e97576797f9c81a49215b9`); this document adds break-and-revert proof,
authoritative CI pytest, and Gate combined-coverage accounting. No production code changes.

<!-- closer-core/v1 repair-of=stranske/Orchestrator#781@782 -->

Refs #781

## Criteria

- [x] Recover complete existing evidence before repeating any work
- [x] Document passing CI pytest results and actual break-and-revert results for each of the four new tests
- [x] Document combined Gate coverage reduction, or the measured shortfall with an explanation for each target
- [x] Record the ranking evidence used to select these targets and reproducible commands

## Recovered artifacts (before local mutation)

| Source | What it preserves |
| --- | --- |
| [PR #782](https://github.com/stranske/Orchestrator/pull/782) body | Local `pytest --cov` deltas, break-and-revert intent, focused validation command |
| CI run [39204843605](https://github.com/stranske/Orchestrator/actions/runs/39204843605) (`headSha` `3b2eba9e299cb4942e81b732de65024078e0164a`) | Green Gate + combined coverage; `gate-coverage/coverage.json` |
| CI run [39231408649](https://github.com/stranske/Orchestrator/actions/runs/39231408649) (merge commit push) | Post-merge combined coverage artifact (same per-target missing lines as PR head) |
| Issue [#781](https://github.com/stranske/Orchestrator/issues/781) body (2026-10-19 dispatch) | Rank order and ranked **uncovered statements+branches** per target |

Prior local branch `closer/issue-781` (`0473506`) held a draft of this file; it was **not** merged.
That draft used pytest-only totals, stale SHAs, and misread the ~180-unit goal as total uncovered mass.
This revision replaces it using CI artifacts and issue dispatch numbers only.

## 1. Pytest (four new tests)

### Local (closer repair worktree, 2026-11-03)

```bash
/opt/anaconda3/bin/python3 -m pytest -q tests/test_coverage_round_781.py -v
```

```
tests/test_coverage_round_781.py::test_completion_adapter_accepts_canonical_identity_and_rejects_secret_payload PASSED
tests/test_coverage_round_781.py::test_keepalive_evidence_dedupes_open_link_and_groups_process_signals PASSED
tests/test_coverage_round_781.py::test_cadence_inspection_reports_retry_and_retirement_without_live_state PASSED
tests/test_coverage_round_781.py::test_research_finding_implementation_requires_a_verified_closing_pr_run PASSED
4 passed in 0.60s
```

### CI (authority for merged tests)

From run `39204843605`, job **verify.py (real pytest counts + selftests + capability gates)**:

```
pytest:     5584 passed, 0 failed, 27/31 max skipped (5611 collected; floor 5607 — 4 BEHIND)
```

Conclusion **success** on the same run. The four tests above are included in that aggregate.

## 2. Deliberate break / restore (2026-11-03, not committed)

Each cycle: single production mutation → `pytest -q <nodeid> --no-cov` (expect fail) → restore file
→ re-run (expect pass). Repository-selected Python: `/opt/anaconda3/bin/python3`.

### 1. `test_completion_adapter_accepts_canonical_identity_and_rejects_secret_payload`

- **Module**: `src/completion_event_adapter.py` — `SECRET_KEYS` check in payload validation
- **Mutation**: disable `if keys & SECRET_KEYS: reasons.append("secret_field_present")`
- **Failure**: `AssertionError` — extra item in expected reason set: `'secret_field_present'`
  (`tests/test_coverage_round_781.py:110`)
- **Restored pass**: 1 passed

### 2. `test_keepalive_evidence_dedupes_open_link_and_groups_process_signals`

- **Module**: `src/keepalive_evidence.py` — open linked-issue dedupe in `_dedupe_candidate`
- **Mutation**: skip setting `possible_duplicate` / `dup_issue` on open link (fall through to search)
- **Failure**: `KeyError: 'possible_duplicate'` at `tests/test_coverage_round_781.py:126` (dedupe
  path not taken; PR #782 described the same break via `issue_search_fn` firing)
- **Restored pass**: 1 passed

### 3. `test_cadence_inspection_reports_retry_and_retirement_without_live_state`

- **Module**: `src/cadence_registry.py` — retirement hold sets `success_status = "retired"`
- **Mutation**: disable `if held and lifted_by is None and success_status != "fresh":` branch
- **Failure**: `AssertionError` — `'missing' != 'retired'` at index 0
  (`tests/test_coverage_round_781.py:173`)
- **Restored pass**: 1 passed

### 4. `test_research_finding_implementation_requires_a_verified_closing_pr_run`

- **Module**: `src/research_subjects.py` — `closure_source` must be in `CLOSURE_SOURCES`
- **Mutation**: disable `closure_source` membership check before `closed_by` handling
- **Failure**: `Failed: DID NOT RAISE ValueError` (`tests/test_coverage_round_781.py:200`)
- **Restored pass**: 1 passed

**Summary**: 4/4 mutations failed as expected; all four pass after restoration.

## 3. Gate / combined CI coverage (authoritative)

**Command** (PR run `39204843605`, `.github/workflows/ci.yml` job *verify.py aggregate + combined
coverage*):

`python3 src/verify.py --aggregate shard-evidence --coverage --floor-may-lag`

(with runner temp `ORCH_STATE_DIR` / `ORCH_LOCAL_RUNTIME`).

**Artifacts** (`gh run download … -n gate-coverage`):

| Role | Run ID | `headSha` (short) |
| --- | ---: | --- |
| PR #782 head (green) | 39204843605 | `3b2eba9` |
| Post-merge push | 39231408649 | `2ea9b54` |

`coverage.json` meta: `branch_coverage` **false** — comparable field is **missing statement lines**
per file (same limitation as [EVIDENCE_778.md](../../EVIDENCE_778.md)).

### Ranked baseline at issue dispatch (statements+branches)

From issue #781 body (coverage-autopilot chunk, lane B):

| Target | Ranked uncovered s+b at dispatch |
| --- | ---: |
| `src/completion_event_adapter.py` | 53 |
| `src/keepalive_evidence.py` | 87 |
| `src/cadence_registry.py` | 10 |
| `src/research_subjects.py` | 86 |
| **Sum (four targets)** | **236** |

### Combined CI missing lines after PR #782 (artifact `39204843605` / `39231408649`)

| File | Missing lines (combined pytest + selftests) |
| --- | ---: |
| `src/completion_event_adapter.py` | 50 |
| `src/keepalive_evidence.py` | 80 |
| `src/cadence_registry.py` | 10 |
| `src/research_subjects.py` | 86 |
| **Sum (four targets)** | **226** |

### Measured reduction vs dispatch ranking

Using issue dispatch counts as pre-chunk baseline and CI artifact as post-merge authority:

| File | Dispatch s+b | Post missing lines | Δ (dispatch − post lines) |
| --- | ---: | ---: | ---: |
| `src/completion_event_adapter.py` | 53 | 50 | **3** |
| `src/keepalive_evidence.py` | 87 | 80 | **7** |
| `src/cadence_registry.py` | 10 | 10 | **0** |
| `src/research_subjects.py` | 86 | 86 | **0** |
| **Four-target total** | **236** | **226** | **10** |

**Pairwise artifact on main immediately before/after PR head** (run `39245366831` vs `39204843605`):
per-target missing lines are **unchanged** (50 / 80 / 10 / 86) — the new tests overlap paths already
counted by existing pytest/selftest mass; the dispatch-vs-post gap above is the honest CI delta.

### Shortfall vs ~180 combined-unit chunk goal

- **Goal** (issue #781): cover ~180 ranked **statements+branches** in one chunk across these four files.
- **Measured CI delta (dispatch ranking → post #782 artifact)**: **10** fewer missing statement lines
  on the four targets (3 + 7 + 0 + 0).
- **Why partial is expected**:
  - **`completion_event_adapter.py`**: Hermetic envelope test covers identity + secret/artifact rejection;
    remaining mass is other phases, capability-effect validation, and adapter branches only selftests hit.
  - **`keepalive_evidence.py`**: Test covers open-link dedupe and `_process_signals` grouping; most file
    mass is GitHub fetch, file I/O, and full candidate pipelines (mocked by design).
  - **`cadence_registry.py`**: Test covers retired-step inspection and retry state; ranked gap was already
    **10** units — combined CI still shows **10** missing lines (registration/CLI/selftest edges).
  - **`research_subjects.py`**: Test covers `closure_source` gate on finding implementation; subject
    lifecycle, experiment aggregation, and CLI remain outside the four-test scope.
- PR #782 reported higher local `pytest --cov` lifts; those are **not** authoritative vs combined Gate
  (see issue scope note on pytest-only baselines).

No production defect was found; expanding tests only to chase the 180 threshold would duplicate
selftest/CLI coverage without new discriminating behavior.

## 4. Ranking basis and reproducible commands

### Ranking source (preserved)

1. Issue [#781](https://github.com/stranske/Orchestrator/issues/781) — escaped-defect signal, then
   churn, then uncovered mass; bundled ~180 units; four named targets in order.
2. Fleet context in issue body: ~86.66% blended line+branch; tests-only round.
3. Repository ranking rail: `escaped_defect_priority` + coverage gap inputs consumed by coverage
   autopilot (`<!-- coverage-autopilot:chunk-round -->` in issue body).

### Reproduce ranking snapshot (offline, from a Gate `coverage.json`)

```bash
gh run download 39204843605 --repo stranske/Orchestrator -n gate-coverage -D /tmp/gate-cov-781
/opt/anaconda3/bin/python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path("/tmp/gate-cov-781/coverage.json").read_text())
targets = [
    "src/completion_event_adapter.py",
    "src/keepalive_evidence.py",
    "src/cadence_registry.py",
    "src/research_subjects.py",
]
for path in targets:
    s = data["files"][path]["summary"]
    print(path, "missing_lines", s["missing_lines"], "pct", round(s["percent_covered"], 1))
PY
```

Expected output (PR #782 head artifact):

```
src/completion_event_adapter.py missing_lines 50 pct 83.2
src/keepalive_evidence.py missing_lines 80 pct 78.8
src/cadence_registry.py missing_lines 10 pct 94.4
src/research_subjects.py missing_lines 86 pct 87.3
```

### Focused validation (PR #782)

```bash
/opt/anaconda3/bin/python3 -m pytest -q \
  tests/test_coverage_round_781.py \
  tests/test_completion_events.py \
  tests/test_cadence_period.py \
  tests/test_cadence_retirement.py \
  tests/test_research_control.py \
  tests/test_research_intake.py
```

## Revisions

| Label | SHA |
| --- | --- |
| PR #782 merge | `2ea9b541d16e70df49e97576797f9c81a49215b9` |
| PR #782 head (green CI) | `3b2eba9e299cb4942e81b732de65024078e0164a` |
| Merge parent / PR base | `52232cfdef83daa3f4c9badcdc68f2a973a262ef` |
| Evidence recorded on main line | `origin/main` at closer repair merge |
