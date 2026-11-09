# Issue 1291: measured toolchain audit

Checked 2026-11-09, before implementation, against source
`d2fdd91b4b09f2a9a8572e14cb5270c3c83628d0` and the proposed pin payload
from sync PR #1289, exact head
`a27084d488d77fcfe7c9644ab97c8f5426bf5cfc` (pin-file Git blob
`71e9fc3e4d3f204b80afcc15f14cc041017b4197`).

Stop rule: three repair attempts or 30 minutes from the initial audit,
whichever occurs first. Only repair the reproduced measured-toolchain failure.

## Complete pin comparison

The latest complete recorded measurement is the 2026-10-22 bundle in
`docs/CI_LINT_BASELINE.md`. All nine proposed entries exist in that measurement;
four versions differ. No recorded complete bundle matches the proposed pins.

| Pin | Recorded / source checkout | Proposed #1289 | Result |
| --- | --- | --- | --- |
| BLACK_VERSION | 26.10.1 | 26.10.1 | Matches |
| RUFF_VERSION | 0.17.2 | 0.17.4 | Version mismatch |
| ISORT_VERSION | 9.0.2 | 9.0.2 | Matches |
| DOCFORMATTER_VERSION | 1.7.8 | 1.7.8 | Matches |
| MYPY_VERSION | 2.4.0 | 2.4.1 | Version mismatch |
| PYTEST_VERSION | 9.1.1 | 9.2.0 | Version mismatch |
| PYTEST_COV_VERSION | 7.1.0 | 7.1.0 | Matches |
| PYTEST_XDIST_VERSION | 3.8.0 | 3.8.0 | Matches |
| COVERAGE_VERSION | 7.17.0 | 7.17.2 | Version mismatch |

## Ownership and mirror inputs

Workflows' `docs/ci/WORKFLOWS.md` identifies the consumer's
`.github/workflows/autofix-versions.env` as authoritative; the Workflows sync
manifest excludes it from file copying. Workflows' workflow guide identifies
Maint 52 as the dev-tool pin propagation lane. Maint 68 is the consumer workflow
template sync lane, not the origin of this one-file dev-tool PR.
`docs/CI_LINT_BASELINE.md`, its measurement collector
`scripts/ci_lint_baseline.py`, and `tests/test_ci_gate_config.py` are local
Orchestrator files, absent from the Workflows sync manifest.

`scripts/build_exec_mirror.sh SRC MIRROR` copies `.github/`, `docs/`, `scripts/`
and `tests/` from `git archive HEAD`; uncommitted changes to those trees do not
travel. The local `ci.yml` aggregate job sets `ORCH_SYNC_SCRIPT` to that builder,
`VERIFY_BEFORE_SYNC_TREE='BARE EXEC MIRROR'`,
`VERIFY_BEFORE_SYNC_FLOOR_MAY_LAG=1`, and
`VERIFY_BEFORE_SYNC_AGGREGATE` to downloaded `mirror-evidence`, then runs
`bash scripts/verify_before_sync.sh "$GITHUB_WORKSPACE"`. That wrapper checks
the payload digest before and after verification. No workflow edit is required
for the baseline measurement or regression tests.

## Pre-implementation reproduction and environment limits

No repository virtual environment or `/opt/anaconda3/bin/python3` exists in
this runner. The available Python is
`/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`. A temporary isolated
environment was created with the runner's Python 3.12.16 at
`/tmp/issue-1291-venv` for the intended measurements.

Actual focused command:

```bash
python3 -m pytest -q tests/test_ci_gate_config.py::test_baseline_was_measured_with_the_pinned_versions -m 'not slow'
```

It exits 1 immediately: `No module named pytest`. It does not reach the named
assertion. Installing the complete proposed nine-pin bundle into the temporary
environment fails: the configured package index reports DNS lookup failures.
An explicit PyPI installation also finds no available distributions. Local
package caches contain neither pytest nor Black. The source checkout still has
the old bundle, so running it alone would not reproduce the proposed-pin
assertion even with pytest installed.

Actual measurement command:

```bash
python3 scripts/ci_lint_baseline.py --json
```

It exits 2 and reports all nine pinned packages missing before measuring any
check. This is an environment blocker, not a measured lint count. No measured
marker or baseline count may be updated from this output.

Actual proposed-tool installation command:

```bash
/tmp/issue-1291-venv/bin/python -m pip install 'black==26.10.1' 'ruff==0.17.4' 'isort==9.0.2' 'docformatter==1.7.8' 'mypy==2.4.1' 'pytest==9.2.0' 'pytest-cov==7.1.0' 'pytest-xdist==3.8.0' 'coverage==7.17.2' PyYAML
```

Fresh measurements, full pytest verification, flat aggregate acceptance,
regenerated PR CI, and the next sync-core classification remain pending.
PR #1289 was still at the original head and open with `draft=false` when read.

## Prepared regression and verification status

The existing three parametrized cases in `tests/test_ci_gate_config.py` now
exercise the named gate through real temporary pin/baseline files. For each of
the nine tools they check a complete matching fixture, reject a deliberately
bumped pin, hybrid measurement, or missing measured entry with the exact named
diagnostic, then restore the fixture and call the gate again. The number of
collected cases is unchanged. These cases have not been executed in this runner.

A Python standard-library audit verified all nine proposed entries, the four
mismatches above, zero missing entries, and absence of any complete matching
recorded bundle. The modified test file passes `ast.parse`, and
`git diff --check` passes. Neither result substitutes for pytest execution.

The full-file command
`/tmp/issue-1291-venv/bin/python -m pytest -q tests/test_ci_gate_config.py -m 'not slow'`
also exits 1 because pytest is missing. Both required formatting commands fail
with `black: command not found`:

```bash
black --line-length 100 tests/test_ci_gate_config.py
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

The required formatting gate therefore prohibits committing the Python change.
The regression and this audit remain uncommitted. Updating the local task ledger
was also rejected because `.agents/` is read-only in this run. No implementation
or live acceptance checkbox is claimed complete; the inspection findings above
are verified independently of those blocked steps.
