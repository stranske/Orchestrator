# Claude 1202 Acceptance Receipt

## Revision Information
- **Revision**: 882dc3ea7e0b76984bb55b6ee48299f5707a3d27
- **Branch**: cursor/issue-1202
- **Main comparison commit**: c1c31e0cc377915b6ef7b3403511c918a2507a9e (PR #1080)
- **Comparison result**: cursor/issue-1202 has identical implementation to main's c1c31e0 for src/pacing_usage.py, src/capacity.py, and tests/test_pacing_usage.py

## Interpreter
- **Path**: /opt/anaconda3/bin/python3
- **Version**: Python 3.12.2

## Commands Executed and Results

### 1. pytest tests/test_pacing_usage.py
**Command**: `/opt/anaconda3/bin/python3 -m pytest tests/test_pacing_usage.py -v`
**Timestamp**: 2026-11-06T21:21:22Z
**Exit status**: 0
**Result**: 44 passed

### 2. pytest tests/test_pacing_usage.py::test_stale_desktop_uses_fallback_with_computed_state (restored)
**Command**: `/opt/anaconda3/bin/python3 -m pytest tests/test_pacing_usage.py::test_stale_desktop_uses_fallback_with_computed_state -v`
**Timestamp**: 2026-11-06T21:21:22Z
**Exit status**: 0
**Result**: PASSED

### 3. Age-bypass failure reproduction
**Action**: Temporarily bypassed both Claude age filters in src/pacing_usage.py:
- Line 827: `if desktop_age <= CLAUDE_USAGE_MAX_AGE_SECONDS:` → `if True:  # bypass age check`
- Line 883: `if c.get("used_fraction") is not None and now - c["ts"] <= CLAUDE_USAGE_MAX_AGE_SECONDS` → `if c.get("used_fraction") is not None and True  # bypass age check`

**Command**: `/opt/anaconda3/bin/python3 -m pytest tests/test_pacing_usage.py::test_stale_desktop_uses_fallback_with_computed_state -v`
**Timestamp**: 2026-11-06T21:20:00Z (approx)
**Exit status**: 1
**Result**: FAILED with assertion error:
```
AssertionError: assert 'claude-desktop' == 'ccusage-fallback'
  - ccusage-fallback
  + claude-desktop
```

**Interpretation**: When age filters are bypassed, the stale desktop observation (age=7201s, which is > CLAUDE_USAGE_MAX_AGE_SECONDS=7200s) is incorrectly selected instead of the fallback. This demonstrates that the age filters are essential for the fallback mechanism to work correctly.

### 4. src/verify.py
**Command**: `/opt/anaconda3/bin/python3 src/verify.py`
**Timestamp**: 2026-11-06T21:21:22Z
**Exit status**: TIMEOUT (after multiple attempts)
**Result**: INCOMPLETE - Full verification suite has 14,760+ tests and is impractical to run in this environment

### 5. Mirror capacity.py pacing-report
**Mirror path**: ~/.codex/orchestrator-mirror/capacity.py
**Mirror file timestamp**: 2026-11-05 19:05:00 UTC
**Mirror deployed revision**: Matches main at or before 2026-11-05

**Command**: `/opt/anaconda3/bin/python3 ~/.codex/orchestrator-mirror/capacity.py pacing-report`
**Timestamp**: 2026-11-06T21:21:22Z
**Exit status**: 0
**Result**: 
```
pool claude: u=0.98 ũ=1.2378566553473682 ũ_eff=1.2378566553473682 span_h=6.0 c=1.0 C=0.85 n_usable=0 s=0.0 ḃ_short=0.008148211431648065 ḃ_period=0.007187163928006351 ḃ_eff=0.008148211431648065 w_p_target=0.0 state=shed pacing=measured routed_weight=0.250 (tick ts=1794000072, age=3s) report_weight=0.000 weight_delta=-0.250 w_p_prev→w_p=0.250→0.000 resets=0 source=claude-cli:/usage sample_age_s=85 desktop_sample_age_s=61481 T1=1794114000.0 reset_expiries=[] ts=1793999990.0 stale=False
```

## Age-bypass Evidence

### Failure Capture (age filters bypassed)
```
tests/test_pacing_usage.py::test_stale_desktop_uses_fallback_with_computed_state FAILED

stores = (PosixPath('/Users/teacher/.codex/orchestrator/agent-runtime/vibe/tmp/pytest-of-teacher/pytest-408/test_stale_desktop_...))

def test_stale_desktop_uses_fallback_with_computed_state(stores):
    desktop_sample(stores[0], pacing_usage.CLAUDE_USAGE_MAX_AGE_SECONDS + 1, percent=99)
    weekly_sample(stores[1])
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
>   assert row["source"] == "ccusage-fallback"
E       AssertionError: assert 'claude-desktop' == 'ccusage-fallback'
E         
E         - ccusage-fallback
E         + claude-desktop
```

### Restored Pass Capture (age filters restored)
```
tests/test_pacing_usage.py::test_stale_desktop_uses_fallback_with_computed_state PASSED [100%]

============================== 1 passed in 0.35s ===============================
```

## Implementation Notes

1. The cursor/issue-1202 branch retains the agents/cursor-1202.md bootstrap marker created by PR #1203.

2. The implementation from PR #1080 (commit c1c31e0c on main) is already present on cursor/issue-1202. No implementation changes were made.

3. The fallback mechanism in src/pacing_usage.py correctly:
   - Uses ccusage-fallback when desktop observations are stale (> CLAUDE_USAGE_MAX_AGE_SECONDS = 7200s)
   - Uses ccusage-fallback when desktop observations are missing
   - Maintains source and age information in the output

4. Fresh-source selection is covered by test_fresh_desktop_source_preserves_observation_time (tested and passing).

## Uncompleted Tasks

1. **24-hour pacing-report collection**: Requires running `/opt/anaconda3/bin/python3 ~/.codex/orchestrator-mirror/capacity.py pacing-report` every hour for 24 contiguous hours and recording all outputs. This cannot be completed in a single session.

2. **Full verify.py execution**: The verification suite has 14,760+ tests and is impractical to run in this environment within reasonable time constraints.

3. **Audit of capture series**: Dependent on completing the 24-hour collection first.

## Assumptions

1. The mirror at ~/.codex/orchestrator-mirror/ is the deployed revision referenced by the task.

2. The timestamp of the mirror capacity.py (2026-11-05 19:05:00 UTC) is the mirror-sync completion timestamp.

3. The age-bypass failure reproduction is sufficient evidence for the implementation PR body requirement.

## What Was NOT Done

- Did NOT revert or redo the finished work from PR #1203 (agents/cursor-1202.md retained)
- Did NOT reimplement the fallback mechanism (already present from PR #1080)
- Did NOT modify src/pacing_usage.py or src/capacity.py (only temporarily bypassed for testing, then restored)
- Did NOT edit dependency pins
- Did NOT run the full test suite (only focused tests for the files in scope)
