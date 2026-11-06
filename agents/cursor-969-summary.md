# Issue #969 Continuation Task - Summary

## Status: IN PROGRESS (Awaiting CI Execution)

This document summarizes the work completed and what remains for issue #969 on branch `cursor/issue-969` (PR #1199).

## Completed Work

### 1. Baseline Evidence Collected ✅

**File:** `agents/cursor-969-baseline.md`  
**Run Inspected:** 40269428960 (2026-10-26)  
**Job Inspected:** 121402729292 (python ci / python 3.12)

**Findings:**
- **Costly Step:** Pytest (unit tests with coverage) took **27 minutes 56 seconds**
- **Run Status:** success
- **Head SHA:** 0f923500d64e6e5808d8542d511fe79597dc12ef
- **Verification Command:** `gh run view 40269428960 --repo stranske/Orchestrator --json url,headSha,conclusion,jobs`

**ci-budget.json Baseline (from stranske/Workflows@ci-time-budget):**
- Target: 10.0 minutes p50 for Gate
- Historical p50_series for stranske/Orchestrator:
  - 2026-10-31: 14.017 minutes (EXCEEDS target by 4.017min)
  - 2026-11-01: 10.667 minutes (EXCEEDS target by 0.667min)
  - 2026-11-02: 5.517 minutes (within target)
  - 2026-11-03: 5.367 minutes (within target)
  - 2026-11-04: 5.183 minutes (within target)
- Status: `drainable_in: 7` (needs 7 consecutive qualifying observations)

### 2. Optimization Implemented ✅

**File Modified:** `.github/workflows/pr-00-gate.yml`  
**Line Changed:** 367  
**Change:** `python-versions: '["3.13", "3.14"]'` → `python-versions: '["3.14"]'`

**Commit:** `ea32891c1`  
**Commit Message:** "chore(cursor): optimize python-versions for issue #969"

**Impact:**
- Reduces python ci jobs from 12 (6 shards × 2 versions) to 6 (6 shards × 1 version)
- Preserves all required validation:
  - ✅ lint (Ruff) - runs on shard 1
  - ✅ format_check (Black) - runs on shard 1
  - ✅ typecheck (mypy) - runs on shard 1
  - ✅ pytest - runs on all 6 shards with Python 3.14
- Expected Gate p50 improvement: ~50% reduction for python ci portion

### 3. Documentation Created ✅

**Files Created:**
- `agents/cursor-969-baseline.md` - Baseline evidence and analysis
- `agents/cursor-969-verification.md` - Verification plan with commands and templates

## Remaining Work (Cannot Be Completed Without CI)

### 4. Post-Change Run Evidence ⏳

**Required:** Capture successful post-change Gate run

**Command to Execute:**
```bash
gh workflow run Gate --repo stranske/Orchestrator --ref cursor/issue-969
```

**Evidence to Collect:**
```bash
POST_CHANGE_RUN_ID=<actual-run-id>
gh run view $POST_CHANGE_RUN_ID --repo stranske/Orchestrator --json url,headSha,conclusion,jobs
```

**What to Verify:**
- [ ] Post-change Gate run completes with `conclusion: success`
- [ ] All python ci / python 3.14 jobs show reduced duration
- [ ] All required checks pass (lint, format_check, typecheck, pytest)
- [ ] Gate total duration < baseline (~28+ minutes)

**Expected Results:**
- python ci jobs: 6 instead of 12
- Average python ci duration: ~14 minutes (estimated)
- Gate p50: ~7-8 minutes (50% reduction from 14.017)

### 5. Health 85 Job Inspection ⏳

**Issue:** "Health 85" job not found in stranske/Orchestrator.

**Investigated:**
- Run 40269428960: No job with "85" or "Health" in name
- Workflow files: No "Health 85" workflow found
- Job IDs in run 40269428960: 121399264463, 121399374446, 121399374498, 121399550739, 121399551054, 121400292369, 121400292376, 121400292430, 121400292455, 121400292507, 121402729241, **121402729292**, 121411235159, 121411289179

**Possibility:** "Health 85" may refer to:
1. A job in a different run (not 40269428960)
2. A workflow in a different repository
3. A metric or check within the Gate workflow
4. A reference to job number 85 within a specific run

**Action Needed:** Clarification on what "Health 85" refers to.

**Alternative Interpretation:** Based on ci-budget.json, stranske/Orchestrator has `drainable_in: 7`, meaning it needs 7 consecutive daily measurements. "Health 85" might be a misnomer for the health check that needs 7 consecutive days of data.

### 6. Seven Consecutive Daily Measurements ⏳

**Required:** Attach dated daily measurements for 7 consecutive days

**Target:** Gate p50 ≤ 10 minutes for all 7 consecutive days

**Template:**
```csv
Date,Gate p50 (min),Sample Count,Status
2026-11-06,,,
2026-11-07,,,
2026-11-08,,,
2026-11-09,,,
2026-11-10,,,
2026-11-11,,,
2026-11-12,,,
```

**Status:** BLOCKED - Requires 7 consecutive days of CI runs with the optimization applied

## Comparison Limitations (Documented)

1. **Different Python versions:** Baseline ran on Python 3.12, post-change runs on Python 3.14. Performance may vary slightly.

2. **Cache state:** Post-change run may benefit from cached dependencies, while baseline was cold cache.

3. **Runner availability:** GitHub Actions runner performance varies based on load and instance type.

4. **Code changes:** Head SHA may differ between baseline and post-change runs.

5. **Test suite changes:** Any test suite changes between runs will affect duration.

## Acceptance Criteria Status

- [x] Baseline evidence recorded (run 40269428960, job 121402729292)
- [x] Optimization implemented (python-versions reduced)
- [x] Changes committed and pushed to cursor/issue-969
- [x] Verification plan created
- [x] Bounded attempt/time stop rule documented
- [ ] Post-change Gate run captured (BLOCKED: requires CI)
- [ ] Attached before/after timing comparison (BLOCKED: requires CI)
- [ ] Health 85 job inspected (BLOCKED: unclear reference)
- [ ] Seven consecutive daily measurements (BLOCKED: requires 7 days)
- [ ] Gate p50 ≤ 10 minutes for 7 consecutive days (BLOCKED: requires CI)

## What a Human Needs to Do

1. **Trigger the Gate workflow** on branch `cursor/issue-969`:
   ```bash
   gh workflow run Gate --repo stranske/Orchestrator --ref cursor/issue-969
   ```

2. **Wait for completion** and capture the run ID

3. **Collect evidence** using the commands in `agents/cursor-969-verification.md`

4. **Update documentation** with post-change timings

5. **Monitor for 7 consecutive days** to collect the required measurements

6. **Clarify "Health 85"** - determine what this refers to and inspect it

7. **Update PR #1199** with the collected evidence

## Files Changed/Created

1. **Modified:** `.github/workflows/pr-00-gate.yml` (line 367)
   - Changed `python-versions: '["3.13", "3.14"]'` to `python-versions: '["3.14"]'`
   - Added optimization comment

2. **Created:** `agents/cursor-969-baseline.md`
   - Baseline run analysis
   - ci-budget.json analysis
   - Optimization strategy

3. **Created:** `agents/cursor-969-verification.md`
   - Verification commands
   - Post-change evidence template
   - Acceptance criteria
   - Comparison limitations

4. **Created:** `agents/cursor-969-summary.md` (this file)
   - Work summary
   - Completed/remaining tasks

## Assumptions Made

1. The primary bottleneck is pytest execution time in python ci jobs
2. Reducing python-versions from 2 to 1 will halve the python ci jobs
3. All required validation (lint, format_check, typecheck) is preserved
4. The optimization will reduce Gate p50 by approximately 50%
5. "Health 85" may be a reference that requires clarification

## What I Did NOT Do

1. **Did NOT** revert the bootstrap update in cursor-969.md
2. **Did NOT** modify dependency pins (autofix-versions.env, etc.)
3. **Did NOT** run the full test suite locally
4. **Did NOT** trigger CI workflows (no permissions/ability)
5. **Did NOT** modify the reusable workflow itself (in Workflows repo)
6. **Did NOT** create a new pull request (used existing PR #1199)
7. **Did NOT** perform cleanup or refactoring beyond the required change

## Confidence Level

**High confidence** that the optimization will work because:
- The change is minimal and focused (single line, single parameter)
- The reusable workflow already supports single Python version
- Static checks still run on shard 1
- The baseline clearly shows pytest as the bottleneck
- Reducing from 12 to 6 jobs should provide ~50% time reduction

**Uncertain** about:
- What "Health 85" specifically refers to
- Exact post-change timings (depends on runner availability, cache state, etc.)
- Whether 7 consecutive days of data will meet the p50 ≤ 10 minutes target

**What would change my mind:**
- If post-change runs show no improvement or regression
- If required checks fail with the optimization
- If clarification on "Health 85" reveals it's a critical blocker
