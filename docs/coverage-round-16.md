# Coverage round 16: acceptance evidence for PR #1252

Checked **2026-11-08 UTC**. This report completes the evidence for
[#1248](https://github.com/stranske/Orchestrator/issues/1248); it adds no tests or
production changes to the already merged [#1252](https://github.com/stranske/Orchestrator/pull/1252).

## Revisions and passing pytest evidence

GitHub's PR metadata reports `merged=true`, merged at `2026-11-08T17:45:19Z`:

- Actual squash merge: `641f88a59d4a16e4aff7b5a209af65333483dfbf`.
- Actual first parent: `1bcfc20dab41bfffd49d24d6f57b29f71bc1cdae` (`git rev-parse 641f88a5^`).
- Original PR head: `a750461a22eeed559bb1deb2a87928681cd83d53`.
- Repair started from current main `f141cf330a4a303276e7b04d31bc9e3a73d77ca0`.

`git diff --stat a750461a 641f88a5` is empty: the tested PR-head tree equals the
actual merge tree. `git diff --stat 1bcfc20d 641f88a5` reports only the two test
modules, 82 insertions. Production targets, coverage configuration, verify runner,
and CI workflow are unchanged between those revisions.

Read-only inspection of [CI run 42504292554](https://github.com/stranske/Orchestrator/actions/runs/42504292554)
verified all six `pytest + coverage shard N/6` jobs and the
[aggregate job 128805444144](https://github.com/stranske/Orchestrator/actions/runs/42504292554/job/128805444144)
concluded **success**. The aggregate log identifies synthetic PR merge `c7dfe01`
(merging the original head into the actual parent) and prints:

```text
pytest:     15042 passed, 0 failed, 31/31 max (15075 collected; floor 15073 — 2 BEHIND)
```

This is passing pytest evidence, not a claim that every selftest ran: the same log
reports an adapter selftest section skipped because an exact Codex profile binary
was unavailable. The floor was allowed to lag in CI; this repair changes no floor.

No repository virtual environment was present. The owner-selected fallback was
`/opt/anaconda3/bin/python3`: Python 3.12.2, pytest 9.1.1, coverage 7.16.2.
The required focused run on the repair branch was:

```sh
ORCH_STATE_DIR=/tmp/closer-r2-1248-state \
ORCH_LOCAL_RUNTIME=/tmp/closer-r2-1248-runtime \
/opt/anaconda3/bin/python3 -m pytest \
  tests/test_adapters_coverage.py tests/test_issue_readiness_coverage.py
```

```text
collected 18 items
18 passed in 0.70s
```

## Gate command and comparable local measurements

At the merge revision, [CI](https://github.com/stranske/Orchestrator/blob/641f88a59d4a16e4aff7b5a209af65333483dfbf/.github/workflows/ci.yml)
runs these commands (recorded here, **not executed locally**):

```sh
python3 src/verify.py --pytest-shard N/6 --coverage --shard-output shard-results
python3 src/verify.py --aggregate shard-evidence --coverage --floor-may-lag
```

Gate downloads the exact-head combined CI artifact. `src/verify.py:child_argv`
inserts `-m coverage run --parallel-mode`; `run_pytest` supplies the flags below.
`pyproject.toml` selects `source=["src"]`, `relative_files=true`, `parallel=true`,
and **`branch=false`**, with its existing omit/exclude rules. Thus the issue's
“statements+branches” label does not mean this Gate actually measured branches.
An absent branch count is **unmeasured**, not zero uncovered branches.

Two complete `git archive` snapshots preserved each revision's existing tests:

```sh
mkdir -p /tmp/closer-r2-1248-evidence/{parent,merge}
git archive 1bcfc20dab41bfffd49d24d6f57b29f71bc1cdae | \
  tar -x -C /tmp/closer-r2-1248-evidence/parent
git archive 641f88a59d4a16e4aff7b5a209af65333483dfbf | \
  tar -x -C /tmp/closer-r2-1248-evidence/merge
```

For each `revision` (`parent`, `merge`) and `profile` (`gate`, `branch`), from its
snapshot directory, the following commands used the same runtime, module
selection, pytest flags, and repository configuration. `gate` used no extra flag;
`branch` added only `--branch` to obtain supplemental branch counts. No parent
test was removed or copied from the merge. Plugin autoload was disabled equally
on both sides. Like the CI child runner's default, these runs used no xdist workers.

```sh
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export ORCH_STATE_DIR=/tmp/closer-r2-1248-evidence/state/$revision/$profile
export ORCH_LOCAL_RUNTIME=/tmp/closer-r2-1248-evidence/runtime/$revision/$profile
export COVERAGE_FILE=/tmp/closer-r2-1248-evidence/.coverage-$revision-$profile
/opt/anaconda3/bin/python3 -m coverage run --parallel-mode \
  -m pytest -q -rfEs -p no:cacheprovider --no-header \
  tests/test_adapters_coverage.py tests/test_issue_readiness_coverage.py
# Supplemental profile: insert --branch after --parallel-mode above.
/opt/anaconda3/bin/python3 -m coverage combine --quiet /tmp/closer-r2-1248-evidence
/opt/anaconda3/bin/python3 -m coverage json --quiet \
  -o /tmp/closer-r2-1248-evidence/$revision-$profile.json
```

All commands exited 0. The test output was:

```text
parent gate:   16 passed in 2.87s
parent branch: 16 passed in 1.35s
merge gate:    18 passed in 1.08s
merge branch:  18 passed in 0.75s
```

Each archive also printed `tracked test inputs: NOT CHECKED` because it has no
`.git` directory. Inputs came directly from `git archive`, not an untracked test
selection. Each test execution took under five seconds including process startup;
no full suite, release pipeline, or `verify.py` invocation ran locally.

Counts below are `files[path].summary.missing_lines` and `missing_branches` from
the supplemental JSON. The statement counts also match the Gate-settings profile
exactly. Source denominators are unchanged: adapters 1551 statements/606 branches;
issue_readiness 550 statements/166 branches.

| Target | Parent missing statements | Merge missing statements | Parent missing branches | Merge missing branches | Reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| `src/adapters.py` | 1202 | 1168 | 524 | 514 | 34 + 10 = **44** |
| `src/issue_readiness.py` | 306 | 300 | 93 | 90 | 6 + 3 = **9** |
| Total | 1508 | 1468 | 617 | 604 | 40 + 13 = **53** |

The focused statement-plus-branch uncovered count fell **2125 → 2072**, a
reduction of **53**, so its numerical shortfall against a reduction of 180 is
**180 − 53 = 127**. With unchanged Gate settings (statements only), the measured
focused reduction is **40**, shortfall **140**. The goal is a *reduction*, not a
ceiling of 180 remaining units. These focused runs omit other pytest modules and
selftests; neither reduction establishes the whole-CI delta.

The downloaded PR [gate-coverage artifact 13627366004](https://github.com/stranske/Orchestrator/actions/runs/42504292554/artifacts/13627366004)
(coverage 7.17.2, `branch_coverage=false`) reports **141** missing adapter statements
and **32** missing issue-readiness statements. The issue's older ranked baseline
was 142 + 38 = 180; that comparison yields **7 fewer statements**, **173 short of
180**. It is an older-baseline comparison, not an exact-parent PR effect. The
connector's PR-run lookup returned no merge-commit run, and read-only
`gh run list --repo stranske/Orchestrator --workflow ci.yml --commit
1bcfc20dab41bfffd49d24d6f57b29f71bc1cdae --json databaseId,headSha,conclusion,url --limit 3`
returned `[]`. Therefore the exact-parent whole-CI reduction and its shortfall
remain **unmeasured**. The bounded replacement is the comparable focused pair
above, not an unauthorized full local run.

## Ranking and why the delivery was partial

[#1248's Scope](https://github.com/stranske/Orchestrator/issues/1248) cites the
repository's escaped-defect → churn → uncovered-mass ranking, from combined CI
`main @ 4cd9bbe4 (2026-11-08T09:15:20Z)`:

| Order | Target | Escaped-defect signals | Churn | Ranked uncovered units |
| --- | --- | ---: | ---: | ---: |
| 1 | `src/adapters.py` | 5 | 14 | 142 |
| 2 | `src/issue_readiness.py` | 3 | 8 | 38 |

#1252 followed this target order. Its deviation was depth: one new test per
target, rather than exhausting the issue's listed missing statements. There is
no evidence that those remaining lines are all unreachable or require live APIs.

**Adapters:** the original opener's real selftest run stopped in
`_selftest_workspace_never_broad`, at the assertion expecting `default state dir`:

```text
$ /opt/anaconda3/bin/python3 src/adapters.py --selftest
assert reason and "default state dir" in reason, reason
AssertionError: /Users/teacher/.codex/orchestrator contains the user's home (/Users/teacher/.codex/orchestrator/agent-runtime/codex/tmp/tmpkpl1iem9/home)
```

The selftest relocated HOME beneath the real orchestrator directory. Checking
the account's orchestrator path then returned the broader “contains the user's
home” reason before the expected default-state classification. This blocked
that local selftest, not the hermetic pytest tests or proof that further unit
tests are impossible. No production defect was established by a new failing
test. This repair preserves that boundary and does not refactor the predicate.

**Issue readiness:** the historical run explicitly passed:

```text
issue_readiness.py selftest: OK (fail-closed risk w/ break->revert, thin issues go to the machine not the owner, exclusions counted, dry-run writes nothing)
issue_readiness_selftest=0
```

The opener stopped expanding coverage after implementing the task-label JSON
route, validated it, and submitted a partial PR. Its transcript supplies **no
technical blocker and no further reason for stopping this target**. Why the
author chose to stop there is unknown; the adapter failure cannot explain it.
The temporary state/runtime fixture does not prevent mocked CLI or GitHub error
paths. Claims that hermetic isolation, authentication, or a flag matrix made
further issue-readiness coverage impossible are unsupported.

## Existing mutation transcripts retained

Source: original opener session `01a1bc11-59e0-70a3-97ba-966dd31fc91a`, turn
`01a1bc17-7ae9-7312-b339-92ae8e9c179c`, checked read-only, corroborated by
[#1252's Break-and-revert proof](https://github.com/stranske/Orchestrator/pull/1252).
These are historical output excerpts, not new mutation runs in this repair.

Adapter mutation: replace
`out["model_label"] = row.get("model") or out["model_label"]` with
`out["model_label"] = None`.

```text
$ PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/anaconda3/bin/python3 -m pytest tests/test_adapters_coverage.py::test_catalog_probe_and_auth_health_keep_observed_evidence -q
E         {'model_label': None} != {'model_label': 'Observed'}
FAILED tests/test_adapters_coverage.py::test_catalog_probe_and_auth_health_keep_observed_evidence
1 failed in 0.23s
RESULT=1
```

Issue-readiness mutation: replace `gaps = task_label_gaps(issues)` with `gaps = []`.

```text
$ PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/anaconda3/bin/python3 -m pytest tests/test_issue_readiness_coverage.py::test_task_label_cli_reports_candidates_without_writing -q
E         {'candidates': 0} != {'candidates': 1}
E         {'rows': []} != {'rows': [{'target': 'fixture#9', 'title': 'Legacy removal', 'label': 'refactor', 'routes_from': 'implement', ...}]}
FAILED tests/test_issue_readiness_coverage.py::test_task_label_cli_reports_candidates_without_writing
1 failed in 0.22s
RESULT=1
```

After reverting both one-line mutations, the original run printed:

```text
$ PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/anaconda3/bin/python3 -m pytest tests/test_adapters_coverage.py tests/test_issue_readiness_coverage.py -q
18 passed in 2.11s
```

These two proofs discriminate stream model capture and candidate reporting.
They do not establish additional mutation proofs for catalog persistence or auth
handling. Those assertions remain in #1252 and pass in the focused and CI runs.
