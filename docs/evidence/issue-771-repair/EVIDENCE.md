# stranske/Orchestrator#771 Closer Repair Evidence

<!-- closer-core/v1 repair-of=stranske/Orchestrator#771@772 -->

**Refs #771**

## Acceptance criteria (repair remainder)

- [x] Record passing CI pytest evidence for the original merged tests, identifying the tested commit and run.
- [ ] Provide comparable Gate coverage measurements attributable to the original five tests, including uncovered statements and branches, measured shortfall, and target-specific limitations. (Statements: quoted Gate figure in §2, raw JSON not preserved. Branches: not measured — historical config sets `branch = false` at both commits; see [coverage/SUMMARY.md](coverage/SUMMARY.md).)
- [x] Retain all five mutation proofs and clearly distinguish original tests from later additions.
- [x] Preserve the explicit unknown historical ranking provenance without presenting inferred reasons as verified selection evidence.

## Scope: PR #772 original five tests vs later additions

PR #772 (merge commit `b1511de27d025a65b4424144a405fd2625ffca28`, PR head `18ad97a054dab7f6039dd42c8131bee04267748b`) added **five** tests in `tests/test_feedback_registry_queries.py`:

| # | Test (PR #772) | Mutation proof source |
|---|----------------|------------------------|
| 1 | `test_run_queries_keep_pending_and_mode_scopes_distinct` | This repair: [mutations/test_run_queries_keep_pending_and_mode_scopes_distinct.md](mutations/test_run_queries_keep_pending_and_mode_scopes_distinct.md) |
| 2 | `test_snapshot_json_exports_initialized_tables_to_the_requested_path` | This repair: [mutations/test_snapshot_json_exports_initialized_tables_to_the_requested_path.md](mutations/test_snapshot_json_exports_initialized_tables_to_the_requested_path.md) |
| 3 | `test_evidence_registry_normalizes_citations_and_retires_unused_rows` | This repair: [mutations/test_evidence_registry_normalizes_citations_and_retires_unused_rows.md](mutations/test_evidence_registry_normalizes_citations_and_retires_unused_rows.md) |
| 4 | `test_evidence_proposals_and_approval_require_recurrence_then_reactivate` | This repair: [mutations/test_evidence_proposals_and_approval_require_recurrence_then_reactivate.md](mutations/test_evidence_proposals_and_approval_require_recurrence_then_reactivate.md) |
| 5 | `test_role_selector_metrics_preserve_no_match_and_rejection_signals` | This repair: [mutations/test_role_selector_metrics_preserve_no_match_and_rejection_signals.md](mutations/test_role_selector_metrics_preserve_no_match_and_rejection_signals.md) (distinct from PR #772's original before/after production-fix account) |

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

**Preserved CI logs (recovered 2026-11-11 for #1327, bounded CI-evidence pass):**

Both runs below were verified at head `18ad97a054dab7f6039dd42c8131bee04267748b` via `gh run view <run-id> --json headSha,conclusion,jobs`.

| Workflow | Run | Pytest job | Job ID | Result | Preserved log |
|----------|-----|-----------|--------|--------|---------------|
| CI | [39180639406](https://github.com/stranske/Orchestrator/actions/runs/39180639406) | `verify.py (real pytest counts + selftests + capability gates)` | 117832439061 | success — `5443 passed, 0 failed` | [run-39180639406-job-117832439061.log](ci/run-39180639406-job-117832439061.log) |
| Gate | [39180639803](https://github.com/stranske/Orchestrator/actions/runs/39180639803) | `python ci / python 3.13` | 117832552036 | success — `5429 passed, 41 skipped`; log lists all five `tests/test_feedback_registry_queries.py` test IDs | [run-39180639803-job-117832552036.log](ci/run-39180639803-job-117832552036.log) |
| Gate | [39180639803](https://github.com/stranske/Orchestrator/actions/runs/39180639803) | `python ci / python 3.12` | 117832552676 | success — `5429 passed, 41 skipped` | [run-39180639803-job-117832552676.log](ci/run-39180639803-job-117832552676.log) |

Other jobs in these runs (flat exec-mirror verify, combined coverage, lint/typecheck, etc.) were identified from the same `gh run view` output but their logs were not preserved in this bounded pass; their historical log content is **unknown** beyond the recorded `success` conclusions above. No unavailable log is presented as recovered.

**Focused verification at merge commit (repair instruction, five tests only)** — unpreserved local transcript (no log artifact in [ci/](ci/)); the mutation records in §3 were measured separately at PR head `18ad97a`:

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

From run **39180639406**, artifact **`gate-coverage`** (`coverage.json`, 2026-10-19T00:31Z) — quoted here from the downloaded artifact, which is **not preserved in this repo** and so is not re-verifiable from the tree (see [coverage/SUMMARY.md](coverage/SUMMARY.md)):

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
`src/feedback.py` — **232 uncovered statements+branches** (coverage-autopilot ranking at issue open). That figure mixes statements and branches in one number; it is **not** a Gate-comparable before measure, and no before/after delta is derived from it.

### Comparable delta and shortfall vs ~180-unit goal

| Measure | Before (ranking) | After (CI artifact, PR head) | Delta |
|---------|------------------|------------------------------|-------|
| Missing statements (Gate combined) | Not stored for `3b379f8`; ranking cites **232** combined units (statements+branches, not separable) | **226** statements only | **Not comparable** — the 232 ranking figure mixes statements and branches, so no like-for-like statement delta can be derived from it |
| Missing branches (Gate combined) | Part of the **232** ranking total | Not published in recovered JSON | **Not recoverable** from artifact |

PR #772’s body already stated the round goal was **not** met on Gate authority (local pytest-only `--cov` showed +109 covered statements, explicitly **not** presented as CI). The only supported shortfall statement is bounded: the after-state still has 226 missing **statements alone** (of 2825; 92.0% statement coverage), which by itself exceeds the ~180 combined-unit target; the combined statement+branch shortfall is **unknown**, and no like-for-like before/after delta is measurable from the recovered artifacts (see table above).

### Remaining meaningful coverage limitations (`src/feedback.py`)

- **Module size:** Combined Gate coverage exercises registry/query/selector paths the five tests target; thousands of lines in ingestion, learning, delegation, and provider paths stay unreachable in hermetic tests without broad integration fixtures.
- **Branches:** Uncovered branch arcs inside the exercised functions (e.g. early returns, alternate status paths in evidence approval) are not fully enumerated in the recovered CI JSON; statement missing counts understate branch gaps.
- **Production fix in #772:** One line in role-selector activation reporting was corrected (double-count of invoked events); tests validate metrics but do not by themselves close the fleet coverage gap for the whole module.

---

## 3. Mutation proofs (all five PR #772 tests)

All five deliberate-break records (patch, failure output, restored pass) are
preserved in [mutations/](mutations/) — one file per test ID, measured at PR
head `18ad97a`. Tests 3–4 are summarised inline below; tests 1, 2 and 5 are in
their mutation files, distinct from PR #772's original break-and-revert
account of the production fix.

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

### Tests 1, 2, 5 — repair mutation records

Preserved in [mutations/](mutations/): run-query (`latest_run_id_for_target`
returns `None`), snapshot (`snapshot_json` returns `{}`), and role-selector
(`record_role_selector_event` returns early) — each with patch, failure
output, and restored pass at `18ad97a`. Separately, PR #772's original
**Break-and-revert proof** recorded the role-metric test failing before the
one-line production fix (`invoked: 4` for two events) and passing after
(`invoked: 2`); that production-fix account is not the repair mutation.

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

## 5. Mutation and coverage artifacts (#1327 completion)

- Mutation records (deliberate break -> failure -> restored pass, patch included) for all five original tests: [mutations/](mutations/) — one file per test ID.
- Controlled coverage comparison separating missing statements from missing branches, with both commits' CI/coverage configs: [coverage/SUMMARY.md](coverage/SUMMARY.md). No raw coverage JSON is preserved there; every cell that cannot be re-derived from the preserved configs and logs is marked **unknown / not measured** rather than estimated.
- Acceptance checklist: CI evidence §1 (preserved logs in [ci/](ci/)), mutations (this section), coverage (this section) link to preserved artifacts above. Historical-ranking provenance (§4): verified items cite issue/PR records; unrecovered per-line ranking provenance remains explicitly **unknown**.

## Commands executed for this repair

```bash
# Five focused tests at merge commit b1511de (output in §1)
ORCH_STATE_DIR=/tmp/orch-state-771 ORCH_LOCAL_RUNTIME=/tmp/orch-runtime-771 \
  /opt/anaconda3/bin/python3 -m pytest -q tests/test_feedback_registry_queries.py

# CI artifact recovery (no local full verify.py)
gh api repos/stranske/Orchestrator/actions/runs/39180639406/artifacts
# Downloaded gate-coverage → coverage.json; gate-coverage-trend → coverage-trend.json
# (downloaded JSON quoted in §2; not committed — see coverage/SUMMARY.md)

# Coverage comparison (coverage/SUMMARY.md): config inspection only in this
# session — the isolated-worktree verify.py --coverage runs and raw JSON
# preservation listed in coverage/SUMMARY.md were NOT executed here.
```

No production code changes. This file replaces the incorrect evidence bundled in #1065 (8-test framing, pytest-only `--cov` as Gate accounting, speculative ranking bullets).
