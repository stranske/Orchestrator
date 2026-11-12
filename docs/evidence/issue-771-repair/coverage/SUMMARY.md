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

| Measure | Baseline `3b379f8` | Executed synthetic merge `12c5c585` | Like-for-like delta |
|---|---|---|---|
| Missing statements, `src/feedback.py` (Gate combined) | Not stored; no `gate-coverage` artifact found for this commit — **unknown** | 226 of 2825 statements (2599 covered, 92.0% reported, rounded), parsed from the preserved raw report linked below | **Not measurable** from preserved artifacts |
| Missing branches, `src/feedback.py` (Gate combined) | Not measured (`branch = false`) — **unknown** | Not measured (`branch = false`); the recovered JSON has no `num_branches`/`missing_branches` keys — **unknown** | **Not measurable** |

## What is not inferred

- Issue #771's ranking figure, **232 uncovered statements+branches**, mixes
  statements and branches in one number. It cannot be split after the fact,
  so no statement delta, branch delta, or combined-unit reduction is derived
  from it, and it is not a like-for-like "before" for the 226 missing
  statements above.
- The approximately 180-unit goal is a reduction goal. With no comparable
  baseline, the achieved reduction and shortfall are **unknown**. No comparison
  of remaining missing statements to that reduction target is valid.
- PR #772's local pytest-only figure (+109 covered statements) is not Gate
  accounting and is not used here.

## Raw report and provenance

- Recovery date: 2026-11-11 America/Chicago (2026-11-12 UTC).
- Run: [39180639406](https://github.com/stranske/Orchestrator/actions/runs/39180639406).
- Artifact: `12156286098`, `gate-coverage`, created `2026-10-19T00:32:00Z`.
  [Preserved GitHub metadata](artifact-12156286098-metadata.json) reports
  `expired: false` at recovery.
- Recovery command: `gh api repos/stranske/Orchestrator/actions/artifacts/12156286098/zip`.
- ZIP SHA256 (matches GitHub artifact digest):
  `72c32017a2259a6402ec6a9673501ccb7fa4419155ad256b60cdf26814795e3d`.
- Raw [coverage.json](run-39180639406-artifact-12156286098-coverage.json), extracted
  unchanged from the ZIP; SHA256:
  `fe042d5f9af298beebfd15dc8f6431b763977525eb1317ed9764102c9f822b11`.
- Report metadata: coverage.py `7.16.2`, timestamp
  `2026-10-19T00:31:51.137370`, `branch_coverage: false`.
- Event headSha: `18ad97a054dab7f6039dd42c8131bee04267748b`.
- Executed checkout: synthetic merge
  `12c5c58517863f747972538c9bb17c568bc0bf49`, merging that event head into
  `4951a5a42b48464c4ace9f970c090f109bf12711`. See preserved CI log line 128
  and both Gate logs line 185 in [../ci/](../ci/). This is distinct from the
  eventual main merge `b1511de27d025a65b4424144a405fd2625ffca28`.

The JSON's `src/feedback.py` summary and line arrays establish the statement
figures above. It measures combined pytest+selftest coverage on the synthetic
merge, not the isolated five tests' contribution. The baseline report remains
unavailable, historical branches were not measured, and no new full coverage
run is required to replace these unknowns.
