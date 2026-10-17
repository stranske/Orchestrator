# Issue 660: shared feedback DB resolver follow-up

The exporter previously repeated DB selection inside `resolve_feedback_db()` despite the
task requiring a shared resolver. Selection now lives in `feedback.resolve_db_path()`, and
the exporter calls it directly. The six existing path-binding cases also assert that the
shared resolver is called once. Precedence and the existing DB/version log are preserved.

The export README now describes path selection, the three publication refusal conditions,
the deliberate override, and cadence refusal logging. The architecture paragraph already
describes the guard.

Local verification on 2026-10-17:

- A standard-library CLI harness passed all six path selections: default, `ORCH_STATE_DIR`,
  CLI state directory, runtime override, environment DB override, and CLI DB override.
  Each invocation called the shared resolver once and logged the expected absolute path
  with `source_version=2`. A separate check passed home-directory expansion.
- `PYTHONPATH=src python3 -m unittest discover -s tests -p test_route_weights_publication.py`
  passed its Git publication test and five subtest scenarios using isolated local remotes.
- A manual CLI mutation check replaced `publish_guard` with a callable returning
  `(True, "")`, then restored it. For both missing and empty DBs, the original/restored
  guard returned exit 1 and logged `publish_guard refused: source_version == 0`.
  The bypass failed with `publish_guard must refuse an empty DB before provisioning`.
  This is supplemental evidence; the requested pytest mutation run remains outstanding.
- Syntax parsing of all three changed Python files and `git diff --check` passed.
- `python3 src/route_weights_export.py --selftest` passed.

Environment blockers:

- `python3 -m pytest tests/test_route_weights_export.py -m 'not slow'` cannot start:
  `No module named pytest`.
- `python3 src/verify.py` was started, then interrupted without a verdict. Its required
  pytest phase cannot execute in this environment; full verification remains outstanding.
- Black is absent. Both formatting the changed files and the required repository check
  cannot run. No commit or push is permitted until the formatting gate passes.
- Installing pytest and Black into `/tmp` failed because `pypi.org` does not resolve.
  Neither package was found in the local pip cache.
- `gh pr view` cannot connect to `api.github.com`, so PR body updates and readiness
  verification are unavailable. Record the actual pytest mutation results in the PR body
  once dependencies and connectivity are restored.

The live post-merge publication and its version/ranking acceptance check remain outstanding.
