# Round 8: disposable acceptance environment

Check date: 2026-11-10. Outcome A1 is all 33 focused acceptance tests passing
in the repository-selected Python 3.14.3 venv, provisioned by the actual runtime
bootstrap. Command (under two minutes):

```
.venv/bin/python -m pytest tests/test_issue1232_ranked_coverage.py tests/test_verify_sharding.py -q --no-cov
```

Stop rule: three repair attempts or 30 minutes. Only the reported acceptance
failure is in scope. No full local suite, floor changes, or skip-ceiling changes.

## Reproduction and repair

The supplied disposable interpreter no longer exists. A fresh Python 3.14.3
venv with pytest 9.2.0, matching the runtime bootstrap's previous install set,
reproduces the sharding failure: 1 failed, 16 passed, 5 skipped. Its collection
subprocess exits 2 because three modules require missing `yaml` or `pydantic`.
`missing-dependencies.log` retains the failure excerpt, omitting the node list.
This is a matching reproduction, not recovery of the deleted original log.

CI already installs `requirements.lock`; `_worktree_interpreter` discovers
`requirements-test.txt` but does not discover that lock file directly. The new
test requirements file includes the unchanged lock and the pytest plugins
needed by the literal acceptance command and coverage-worker tests. It adds no
version pins. No bootstrap source change or dependency-lock edit is needed.

The sharding test now asserts the subprocess return code explicitly and includes
stdout/stderr on failure. It still refuses incomplete collection and enforces
exact partitioning; no test was added or skipped to conceal the failure.

The actual `_worktree_interpreter(Path.cwd())`, invoked using the available
Python 3.14.3 runtime, successfully provisioned this checkout's initially absent
`.venv`. The selected interpreter then passed the literal command above
(`acceptance.log`) and the plugin-disabled runtime sharding command
(`runtime-acceptance.log`). The owner fallback also passed all 33 tests.
Black, Ruff, and whitespace checks pass for the changed test.

## Remaining evidence boundary

Starting head `4a88ee29e329f14ad74e20dca94a3a425890fb6e` has successful normal
Gate run 42874311023 and normal/flat verification run 42874310586. Those runs
do not establish A4 for this repair commit; new exact-head CI is required.
Round 7 mutation and combined-coverage records remain unchanged. No production
behavior or coverage target changed in this round.
