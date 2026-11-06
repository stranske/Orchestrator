# Issue #969 Verification Plan

## Change Summary

**Modified:** `.github/workflows/pr-00-gate.yml`  
**Change:** Line 367, reduced `python-versions` from `["3.13", "3.14"]` to `["3.14"]`  
**Commit:** `ea32891c`  
**Branch:** `cursor/issue-969` (PR #1199)

## Post-Change Verification Commands

### 1. Trigger and Monitor Gate Run

```bash
# Trigger Gate workflow manually (workflow_dispatch)
gh workflow run Gate --repo stranske/Orchestrator --ref cursor/issue-969

# Get the run ID of the triggered workflow
gh run list --repo stranske/Orchestrator --workflow Gate --limit 1 --json databaseId

# Monitor the run (replace <run-id> with actual run ID)
gh run watch <run-id> --repo stranske/Orchestrator

# View detailed results
gh run view <run-id> --repo stranske/Orchestrator --json url,headSha,conclusion,jobs
```

### 2. Extract Post-Change Timings

```bash
# Get the post-change run details
POST_CHANGE_RUN_ID=<actual-run-id>
gh run view $POST_CHANGE_RUN_ID --repo stranske/Orchestrator --json url,headSha,conclusion,jobs > post-change-run.json

# Extract python ci job timings
python3 -c "
import json
with open('post-change-run.json') as f:
    run = json.load(f)
    for job in run.get('jobs', []):
        if 'python ci' in job.get('name', '').lower():
            started = job.get('startedAt')
            completed = job.get('completedAt')
            if started and completed:
                # Calculate duration in seconds
                from datetime import datetime
                start = datetime.fromisoformat(started.replace('Z', '+00:00'))
                end = datetime.fromisoformat(completed.replace('Z', '+00:00'))
                duration = (end - start).total_seconds() / 60  # minutes
                print(f'{job[\"name\"]}: {duration:.2f} minutes')
"
```

### 3. Compare Before/After

**Baseline (Run 40269428960):**
- python ci / python 3.12: 27.93 minutes (1676 seconds)
- Gate total: ~28+ minutes
- p50 on 2026-10-31: 14.017 minutes

**Expected Post-Change:**
- python ci jobs: 6 instead of 12 (50% reduction)
- Each python ci / python 3.14 shard: ~14 minutes (estimated, assuming similar workload)
- Gate p50: ~7-8 minutes (50% reduction from 14.017)

### 4. Verify Required Checks Preserved

Check that the following are still executed:
- ✅ lint (Ruff) - runs on shard 1
- ✅ format_check (Black) - runs on shard 1  
- ✅ typecheck (mypy) - runs on shard 1
- ✅ pytest - runs on all 6 shards with Python 3.14

### 5. Health 85 Job Verification

```bash
# Check Health 85 job in the post-change run
python3 -c "
import json
with open('post-change-run.json') as f:
    run = json.load(f)
    for job in run.get('jobs', []):
        if 'Health' in job.get('name', '') or '85' in job.get('name', ''):
            print(f'Health job: {job[\"name\"]}, Conclusion: {job[\"conclusion\"]}')
"
```

## Comparison Limitations

1. **Different Python versions:** Baseline ran on Python 3.12, post-change runs on Python 3.14. Performance may vary slightly between versions.

2. **Cache state:** Post-change run may benefit from cached dependencies, while baseline may have been cold cache.

3. **Runner availability:** GitHub Actions runner performance can vary based on load and instance type.

4. **Code changes:** The head SHA may have different code between baseline and post-change runs.

5. **Test suite changes:** Any changes to the test suite between runs will affect duration.

## Acceptance Criteria

- [ ] Post-change Gate run completes with `conclusion: success`
- [ ] All required checks pass (lint, format_check, typecheck, pytest)
- [ ] python ci / python 3.14 jobs show reduced duration compared to baseline python ci / python 3.12
- [ ] Gate p50 (from ci-budget.json) shows sustained ≤ 10 minutes over 7 consecutive days
- [ ] Health 85 job completes successfully

## Post-Change Evidence Template

Once the post-change run is available, fill in the following:

```
Post-Change Run ID: <run-id>
Post-Change Head SHA: <head-sha>
Post-Change URL: <url>

Post-Change python ci Timings:
- python ci shard 1/6 / python 3.14: X.XX minutes
- python ci shard 2/6 / python 3.14: X.XX minutes
- python ci shard 3/6 / python 3.14: X.XX minutes
- python ci shard 4/6 / python 3.14: X.XX minutes
- python ci shard 5/6 / python 3.14: X.XX minutes
- python ci shard 6/6 / python 3.14: X.XX minutes

Average python ci duration: X.XX minutes
Gate total duration: X.XX minutes

Comparison:
- Baseline Gate: ~28+ minutes
- Post-Change Gate: X.XX minutes
- Improvement: X.XX% reduction
```

## Health 85 Series Tracking

Create dated daily measurements for 7 consecutive days:

```
Date,Gate p50 (min),Sample Count,Status
2026-11-06,,,
2026-11-07,,,
2026-11-08,,,
2026-11-09,,,
2026-11-10,,,
2026-11-11,,,
2026-11-12,,,
```

Target: p50 ≤ 10 minutes for all 7 consecutive days.
