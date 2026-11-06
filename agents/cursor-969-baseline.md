# Issue #969 Baseline Evidence

## Run 40269428960 Analysis

**Date:** 2026-10-26  
**Repository:** stranske/Orchestrator  
**Workflow:** Gate (pr-00-gate.yml)  
**Head SHA:** 0f923500d64e6e5808d8542d511fe79597dc12ef  
**Conclusion:** success  
**URL:** https://github.com/stranske/Orchestrator/actions/runs/40269428960

### Costly Step: Job 121402729292

**Job Name:** python ci / python 3.12  
**Database ID:** 121402729292  
**URL:** https://github.com/stranske/Orchestrator/actions/runs/40269428960/job/121402729292  
**Status:** success  
**Duration:** ~27 minutes 56 seconds (startedAt: 2026-10-26T04:50:00Z, completedAt: 2026-10-26T05:18:24Z)

#### Step-Level Breakdown

The bottleneck step was **Step 19: Pytest (unit tests with coverage)**
- **Started:** 2026-10-26T04:50:19Z
- **Completed:** 2026-10-26T05:18:15Z
- **Duration:** 27 minutes 56 seconds

All other steps completed in seconds:
- Set up job: ~1s
- Mint GitHub App Token: ~1s
- Checkout repository: ~2s
- Set up Python: ~1s
- Set up Node.js: ~1s
- Checkout Workflows artifact cache action: ~1s
- Install uv: ~1s
- Cache uv artifacts: ~1s
- Install dependencies: ~2s
- Validate test dependencies: ~1s
- Check for undeclared test dependencies: ~3s
- Various cache steps: ~1s each
- Post-steps: ~1s each

**Verification Command:**
```bash
gh run view 40269428960 --repo stranske/Orchestrator --json url,headSha,conclusion,jobs
```

**Check Date:** 2026-11-06 (current date at time of inspection)

**Bounded Attempt/Time Stop Rule:**
- Maximum 3 optimization attempts
- Stop if Gate p50 ≤ 10 minutes for 7 consecutive days
- Stop if any required check is broken (lint, format_check, typecheck, pytest)

## ci-budget.json Baseline (from stranske/Workflows@ci-time-budget)

**Target:** 10.0 minutes p50 for Gate

**Historical p50_series for stranske/Orchestrator:**
- 2026-10-31: 14.017 minutes (EXCEEDS target by 4.017min)
- 2026-11-01: 10.667 minutes (EXCEEDS target by 0.667min)
- 2026-11-02: 5.517 minutes (within target)
- 2026-11-03: 5.367 minutes (within target)
- 2026-11-04: 5.183 minutes (within target)
- 2026-11-05: null
- 2026-11-06: null

**Issue #969 Filed:** 2026-10-26T12:27:05Z  
**First Over Target:** 2026-10-26T11:48:48+00:00  
**Status:** drainable_in = 7 (needs 7 consecutive qualifying observations)

## Optimization Strategy

The primary bottleneck is pytest execution time. The python-ci job in pr-00-gate.yml currently:
- Uses python-versions: ["3.13", "3.14"] (2 versions)
- Uses 6 shards (from config/test-shards.json)
- Creates 12 total python ci jobs (6 shards × 2 versions)

**Optimization:** Reduce python-versions from ["3.13", "3.14"] to ["3.14"] only.
- This reduces python ci jobs from 12 to 6
- Preserves all required validation (lint, format_check, typecheck still run on shard 1)
- primary-python-version is already '3.14'
- Expected time reduction: ~50% for python ci portion of Gate
