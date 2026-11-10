# Issue 698 keepalive validation — 2026-11-10

Evaluated committed head: `0e443b2269703ed1814acb79ff7e2d05edbf6575`.
Selected runner interpreter: `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python`
(Python 3.14.10). The historical Python 3.12.2 interpreter in the PR body is
from a different machine.

CI run [42765070132](https://github.com/stranske/Orchestrator/actions/runs/42765070132)
is associated with that PR head and tests its GitHub merge revision `6fd7e1f`:

- [Checkout aggregate](https://github.com/stranske/Orchestrator/actions/runs/42765070132/job/129660702408):
  8,451 tests passed, 104 selftests spoke, 3/5 gates green; 27 tests,
  7 selftests and 2 gates skipped for named missing prerequisites.
- [Flat mirror aggregate](https://github.com/stranske/Orchestrator/actions/runs/42765070132/job/129660614678):
  8,430 tests passed, 104 selftests spoke, 3/5 gates green; 48 tests,
  7 selftests and 2 gates skipped for named missing prerequisites.

Both aggregate jobs concluded `success`. No local full suite, shard or verifier
was run. These CI results predate the working-tree regression extension.

The existing sequential regression now also exercises recovery from zero and
both immediate failure overrides at every fresh observation, for zero and two
resets. Its collected test count is unchanged. A direct production smoke check
at `2026-11-10T06:04:00Z` passed all 128 evaluations (two reset budgets, sixteen
ticks, four paths). This does not establish a pytest-suite or formatting pass.

The required local commands were attempted with the selected interpreter:

```text
python -m pytest tests/test_pacing.py tests/test_pacing_usage.py tests/test_route_weights_export_comparative.py -m 'not slow'
No module named pytest

python -m black --line-length 100 tests/test_pacing.py
No module named black

python -m black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
No module named black
```

Installing pytest and Black into `/tmp/orchestrator-698-test-tools` failed because
`pypi.org` could not resolve. Neither package was found in the local package cache.
The Python changes remain uncommitted because the mandatory Black gate cannot
pass. Resume on a runner with pytest and Black available, run the commands above,
then commit and obtain CI verification for the resulting head.

The runtime criterion remains unchecked: this runner has no installed
`~/.codex/orchestrator-mirror`, runtime state or handoff directory. It cannot
establish a repair deployment, a current weight threshold, or a repair-bound
48-hour ledger window. No runtime settings were changed.

Publishing the CI update to the PR body was also blocked: the GitHub tool returned
`MCP tool call requires approval, but approval policy is never`. The remote body
was not changed; a blocker comment and `needs-human` label could not be published
through that approval-required mutation path. No approval bypass was attempted.
