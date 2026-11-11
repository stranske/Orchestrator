# PR #1350: round 3 disposable-environment acceptance repair

Check date: 2026-11-11. Starting head: `f8ad8393731d`.
Outcome: both reported command checks print `status: PASS` through
`runtime_ac._run_command_check` in a fresh Python 3.14.3 repository venv.
Stop rule: three repair attempts or 30 minutes; one repair attempt used.

The default local interpreter first passed the original combined acceptance
command (33 passed). A clean Python 3.14.3 environment with only pytest 9.2.0
reproduced the reported failure: 1 failed, 27 passed, 5 skipped. The shard-plan
test's nested collection exited 2: two modules import `yaml`, and one imports
`pydantic`, neither installed by the previous pytest-only bootstrap.

Added `requirements-test.txt` declaring PyYAML and pydantic without version
pins. No test assertions, shard assignments, production code, dependency pins,
floors, ceilings, or coverage exclusions changed. No added tests require a new
mutation record; the failure above is the before-repair negative control.

The existing gate bootstrap discovered this file and installed it in a fresh
`.venv`. Reproduce provisioning and the actual two command checks:

```sh
PYTHONPATH=src /Users/teacher/.codex/py314/bin/python -c \
  'from runtime_ac_gate import _worktree_interpreter; print(_worktree_interpreter("."))'
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
from runtime_ac import _run_command_check
import json
import sys
for node in ('tests/test_issue1232_ranked_coverage.py', 'tests/test_verify_sharding.py'):
    result = _run_command_check(
        {'id': node, 'type': 'command', 'command': f'python -m pytest -p no:cov {node}'},
        cwd=Path.cwd(), timeout=120, python_interpreter=sys.executable,
    )
    print(json.dumps(result, indent=2))
    if result['status'] != 'PASS':
        raise SystemExit(1)
PY
```

Results: 11 passed in 0.33s; 17 passed, 5 skipped in 11.59s. The gate deliberately
disables pytest-cov, and its minimal environment lacks coverage, so the five
existing `needs_coverage_module` cases skip. This does not claim those five
coverage checks executed in the minimal environment, or that the literal
`--no-cov` flag is supported without pytest-cov; the runtime runner normalizes
that flag away. The full suite was only collected by the named shard test,
never executed locally. Fresh CI must establish both required layouts on the
pushed commit; earlier-head green CI does not establish that result.
