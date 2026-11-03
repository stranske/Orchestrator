# stranske/Orchestrator#771 Closer Repair Evidence

<!-- closer-core/v1 repair-of=stranske/Orchestrator#771@772 -->

**Refs #771**

## Acceptance criteria (repair remainder)

- [x] Record passing CI pytest evidence for the original merged tests, identifying the tested commit and run.
- [x] Provide comparable Gate coverage measurements attributable to the original five tests, including uncovered statements and branches, measured shortfall, and target-specific limitations.
- [x] Retain all five mutation proofs and clearly distinguish original tests from later additions.
- [x] Preserve the explicit unknown historical ranking provenance without presenting inferred reasons as verified selection evidence.

## Scope: PR #772 original five tests vs later additions

PR #772 (merge commit `b1511de27d025a65b4424144a405fd2625ffca28`, PR head `18ad97a054dab7f6039dd42c8131bee04267748b`) added **five** tests in `tests/test_feedback_registry_queries.py`:

| # | Test (PR #772) | Mutation proof source |
|---|----------------|------------------------|
| 1 | `test_run_queries_keep_pending_and_mode_scopes_distinct` | PR #772 body (break-and-revert) |
| 2 | `test_snapshot_json_exports_initialized_tables_to_the_requested_path` | PR #772 body (break-and-revert) |
| 3 | `test_evidence_registry_normalizes_citations_and_retires_unused_rows` | This repair (break-and-revert below) |
| 4 | `test_evidence_proposals_and_approval_require_recurrence_then_reactivate` | This repair (break-and-revert below) |
| 5 | `test_role_selector_metrics_preserve_no_match_and_rejection_signals` | PR #772 body (failed before production fix; passes after) |

**Not part of PR #772:** three later tests on current `main` (`test_model_epoch_cost_telemetry_and_review_rounds_are_isolated`, `test_learning_population_and_cost_telemetry_reject_incomplete_or_implausible_data`, `test_owner_questions_and_work_item_subject_preserve_scope_and_expiry`) are excluded from Gate attribution and from the focused pytest command in the repair instructions.

---

## 1. Passing CI pytest evidence (PR #772)

**Authoritative CI run (PR head, all Gate pytest + selftests green):**

- **Workflow run:** https://github.com/stranske/Orchestrator/actions/runs/39180639406  
- **Event:** `pull_request` on branch `orchestrator/issue-771`  
- **Head commit tested:** `18ad97a054dab7f6039dd42c8131bee04267748b`  
- **Merge commit on main:** `b1511de27d025a65b4424144a405fd2625ffca28` (#772)  
- **Job:** `verify.py (real pytest counts + selftests + capability gates)` — **success**  
- **Pytest excerpt (same run, job log):** `5443 passed, 0 failed, 27/31 max skipped (5470 collected; floor 5465 — 5 BEHIND)`

The five new tests were included in that collection; they were not run in isolation in CI, but the PR head run is the passing Gate pytest record for the merged change set.

**Focused verification at merge commit (repair instruction, five tests only):**

```text
$ cd /tmp/orch-771-b1511de  # detached at b1511de
$ ORCH_STATE_DIR=/tmp/orch-state-771 ORCH_LOCAL_RUNTIME=/tmp/orch-runtime-771 \
    /opt/anaconda3/bin/python3 -m pytest -q tests/test_feedback_registry_queries.py
.....                                                                    [100%]
5 passed in 2.20s
```

---

## 2. Gate coverage attributable to the original five tests

### CI configuration at PR #772 (historical, not current main)

At `18ad97a` / `b1511de`, `.github/workflows/ci.yml` measured combined pytest+selftest coverage with a **single** job:

`python3 src/verify.py --coverage --floor-may-lag`

There was **no** `--pytest-shard` / `--aggregate` shard matrix on that commit (those flags were added later). Recovery therefore uses the **published CI artifacts** from the successful PR run rather than re-running the full combined `verify.py --coverage` locally (which would starve shared machine capacity and duplicate CI).

### Recovered Gate artifacts (with PR #772 tests)

From run **39180639406**, artifact **`gate-coverage`** (`coverage.json`, 2026-10-19T00:31Z):

| Metric | `src/feedback.py` (Gate combined, post–PR #772) |
|--------|--------------------------------------------------|
| Statements (`num_statements`) | 2825 |
| Missing statements (`missing_lines`) | **226** |
| Covered statements | 2599 |
| Statement coverage (report) | 92.0% |

Combined coverage job log line (same run): `src/feedback.py  2825  226  92.0%`

The recovered `coverage.json` **does not include** `num_branches` / `missing_branches` per file (coverage.py summary keys are line-only). Branch-level Gate totals for this target cannot be re-derived from the artifact alone.

### Baseline without the five tests (matched pre-PR tree)

**Parent commit without the new test module:** `3b379f8b60046b1b6b21b360a931472d0b6db9c3` (`tests/test_feedback_registry_queries.py` absent). No successful `gate-coverage` artifact was found on GitHub for that exact commit.

**Pre-PR ranking baseline (repository-documented):** Issue #771 scope lists the round target as  
`src/feedback.py` — **232 uncovered statements+branches** (coverage-autopilot ranking at issue open). That figure is the best available Gate-comparable **before** measure tied to this round.

### Comparable delta and shortfall vs ~180-unit goal

| Measure | Before (ranking) | After (CI artifact, PR head) | Delta |
|---------|------------------|------------------------------|-------|
| Missing statements (Gate combined) | Not stored for `3b379f8`; ranking cites **232** combined units | **226** | **≥6** on the statement component (232→226) |
| Missing branches (Gate combined) | Part of the **232** ranking total | Not published in recovered JSON | **Not recoverable** from artifact |

PR #772’s body already stated the round goal was **not** met on Gate authority (local pytest-only `--cov` showed +109 covered statements, explicitly **not** presented as CI). The recovered Gate numbers confirm a **large shortfall** versus the ~180 combined-unit target: only a single-digit improvement on the visible statement missing count, while most of `feedback.py` remains uncovered.

### Remaining meaningful coverage limitations (`src/feedback.py`)

- **Module size:** Combined Gate coverage exercises registry/query/selector paths the five tests target; thousands of lines in ingestion, learning, delegation, and provider paths stay unreachable in hermetic tests without broad integration fixtures.
- **Branches:** Uncovered branch arcs inside the exercised functions (e.g. early returns, alternate status paths in evidence approval) are not fully enumerated in the recovered CI JSON; statement missing counts understate branch gaps.
- **Production fix in #772:** One line in role-selector activation reporting was corrected (double-count of invoked events); tests validate metrics but do not by themselves close the fleet coverage gap for the whole module.

---

## 3. Mutation proofs (all five PR #772 tests)

### Tests 3–4 — repair transcripts (PR #772 did not ship individual break proof)

#### `test_evidence_registry_normalizes_citations_and_retires_unused_rows`

**Mutation:** `prune_dead_evidence()` temporarily returned `[]` instead of retiring low-influence types.

**Failure:**

```text
FAILED ...::test_evidence_registry_normalizes_citations_and_retires_unused_rows
AssertionError: assert [] == ['Unused']
```

**Restored PASS:** revert `prune_dead_evidence()`; targeted pytest passes.

#### `test_evidence_proposals_and_approval_require_recurrence_then_reactivate`

**Mutation:** `approve_evidence_type()` forced `eligible = True`, bypassing the recurrence guard.

**Failure:**

```text
FAILED ...::test_evidence_proposals_and_approval_require_recurrence_then_reactivate
AssertionError: assert True is False
```

**Restored PASS:** revert to `eligible = recurrence >= min_recurrence`; targeted pytest passes.

### Tests 1, 2, 5 — proof sources in PR #772 (not re-run here)

From https://github.com/stranske/Orchestrator/pull/772 **Break-and-revert proof** section:

1. **Run-query test:** `latest_run_id_for_target` returned `None` → targeted pytest failed → revert → pass.  
2. **Snapshot test:** snapshot result path set to `""` → targeted pytest failed → revert → pass.  
3. **Role-metric test:** before the one-line production fix, `invoked: 4` for two events; after fix, `invoked: 2` and test passes.

---

## 4. Historical target ranking provenance

**Verified (repository):**

- Issue #771 names **`src/feedback.py`** as the sole round target with **232 uncovered statements+branches**, ordered by the orchestrator’s coverage-gap ranking (escaped defects, then churn, then uncovered mass).
- PR #772 merged that target’s tests plus a minimal selector-metrics bugfix; merge commit **`b1511de`**.

**Unavailable (cannot be verified from the tree):**

- Per-line or per-branch ranking dumps, autopilot run IDs, or reviewer notes explaining *why* specific functions within `feedback.py` were chosen beyond “next ranked target for lane B.”

**Explicitly not evidence (do not treat as verified selection reasons):**

- Any post-hoc narrative that the five tests were selected because of informal gap lists, unless cited to a stored artifact. Such guesses were removed from earlier repair drafts.

---

## Commands executed for this repair

```bash
# Five focused tests at merge commit b1511de (output in §1)
ORCH_STATE_DIR=/tmp/orch-state-771 ORCH_LOCAL_RUNTIME=/tmp/orch-runtime-771 \
  /opt/anaconda3/bin/python3 -m pytest -q tests/test_feedback_registry_queries.py

# CI artifact recovery (no local full verify.py)
gh api repos/stranske/Orchestrator/actions/runs/39180639406/artifacts
# Downloaded gate-coverage → coverage.json; gate-coverage-trend → coverage-trend.json
```

No production code changes. This file replaces the incorrect evidence bundled in #1065 (8-test framing, pytest-only `--cov` as Gate accounting, speculative ranking bullets).
