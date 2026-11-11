# Controlled coverage comparison — issue #771 / PR #772

Commits compared:

- PR head (five tests added): `18ad97a054dab7f6039dd42c8131bee04267748b`
- Baseline (test module absent): `3b379f8b60046b1b6b21b360a931472d0b6db9c3`

## Configurations inspected (preserved in this directory)

| Artifact | What it proves |
|---|---|
| [ci-18ad97a.yml](ci-18ad97a.yml), [ci-3b379f8.yml](ci-3b379f8.yml) | Both commits measure combined coverage in one job: `python3 src/verify.py --coverage --floor-may-lag`. No `--pytest-shard`/`--aggregate` matrix at either commit. |
| [pyproject-18ad97a.toml](pyproject-18ad97a.toml), [pyproject-3b379f8.toml](pyproject-3b379f8.toml) | Both set `[tool.coverage.run] branch = false`, `parallel = true`, `source = ["src"]`, and omit `src/verify.py` and `tests/*`. Branch coverage was therefore **not measured** by the historical configuration at either commit. |
| [verify-18ad97a.py](verify-18ad97a.py), [verify-3b379f8.py](verify-3b379f8.py) | Historical `src/verify.py` snapshots whose `--coverage` mode instruments the pytest process and its `--selftest` subprocesses, then combines the parallel data files. |

Because `branch = false` at both commits, a like-for-like branch comparison
cannot be recovered from the historical configuration or its artifacts.
Reporting `0` missing branches would be wrong: the correct value is
**not measured / unknown**.

## Statements vs branches (kept separate)

| Measure | Baseline `3b379f8` | PR head `18ad97a` | Like-for-like delta |
|---|---|---|---|
| Missing statements, `src/feedback.py` (Gate combined) | Not stored; no `gate-coverage` artifact found for this commit — **unknown** | 226 of 2825 statements (2599 covered, 92.0%), quoted from run 39180639406 `gate-coverage` `coverage.json`; that raw JSON is **not preserved in this directory**, so the figure is not re-verifiable from the tree | **Not measurable** from preserved artifacts |
| Missing branches, `src/feedback.py` (Gate combined) | Not measured (`branch = false`) — **unknown** | Not measured (`branch = false`); the recovered JSON has no `num_branches`/`missing_branches` keys — **unknown** | **Not measurable** |

## What is not inferred

- Issue #771's ranking figure, **232 uncovered statements+branches**, mixes
  statements and branches in one number. It cannot be split after the fact,
  so no statement delta, branch delta, or combined-unit reduction is derived
  from it, and it is not a like-for-like "before" for the 226 missing
  statements above.
- The only supported shortfall statement is bounded: the after-state still
  has 226 missing **statements alone**, which by itself exceeds the
  ~180 combined-unit round goal; how far the combined (statement + branch)
  total remained above the goal is **unknown**.
- PR #772's local pytest-only figure (+109 covered statements) is not Gate
  accounting and is not used here.

## Raw reports

No raw coverage JSON is preserved in this directory. The 2825/226/92.0%
figures above are quoted in [../EVIDENCE.md](../EVIDENCE.md) §2 from the
`gate-coverage` artifact downloaded from run 39180639406; until that
`coverage.json` (and a baseline equivalent, if one ever exists) is committed
here, every cell marked **unknown** above stays unknown rather than estimated.

## Reproduction (for a runner with a shell; not executed in the repair session)

```bash
# Historical statement-only measurement (matches the preserved configs):
git worktree add /tmp/orch-771-18ad 18ad97a054dab7f6039dd42c8131bee04267748b
git worktree add /tmp/orch-771-3b37 3b379f8b60046b1b6b21b360a931472d0b6db9c3
# In each worktree: python3 src/verify.py --coverage --floor-may-lag
# then commit the resulting coverage.json here as a raw report.
# A branch-enabled run (--cov-branch / branch=true) would be a NEW
# measurement under a different configuration, not the historical Gate one.
```
