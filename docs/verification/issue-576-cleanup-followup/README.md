# Cleanup evidence follow-up for source 576

Actual comparison of merged PR586 (`ad61e84e6d585c47084235eed50e84cdc2e115bf`) reported CONCERNS, including a cleanup defect. The original helper ignored a failed `git worktree prune` after fallback directory removal; it also hid cleanup failure behind an already propagating verification error.

Two parameterized regression cases fail on that exact merged implementation, then pass after correction. All39 runtime-shadow tests pass. The helper now reports unsuccessful pruning even when the directory is gone, and chains the original verifier exception as the cleanup exception's cause. No canonical cleanup or network fetch is added.

Command: `python3 -m pytest tests/test_runtime_ac_shadow.py -q`. Exact combined collection3308; every existing ceiling retained. Black/Ruff and focused mypy on provision are checked independently. These are local fixtures, not hosted CI/provider PASS or post-sync production execution.

Source576 remains open for the full actual-comparison disposition and authentic post-sync `executed > 0` evidence. No mirror sync or runtime flag change is included.
