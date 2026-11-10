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

## Follow-up reconciliation — 2026-11-10T06:14:35Z

Reviewed the two recent keepalive commits. Contrary to the earlier note about
uncommitted changes, automation subsequently committed that regression extension
as `2a6fb73109cae5c388b4050e883c36989e9b83d1`. Commit `0e443b22` was the main
integration merge; its infrastructure changes are not new pacing implementation.

CI run [42766212742](https://github.com/stranske/Orchestrator/actions/runs/42766212742)
is associated with committed head `2a6fb731`. Both verifier aggregates succeeded:

- [Checkout aggregate](https://github.com/stranske/Orchestrator/actions/runs/42766212742/job/129664381203),
  at `06:10:20Z`: 8,451 passed, zero failed, 8,480 collected;
  104 selftests spoke and 3/5 gates were green. There were 27 test skips,
  seven selftest skips and two gate skips for named missing prerequisites.
- [Flat mirror aggregate](https://github.com/stranske/Orchestrator/actions/runs/42766212742/job/129664711230),
  at `06:12:28Z`: 8,430 passed, zero failed, 8,480 collected;
  104 selftests spoke and 3/5 gates were green. There were 48 test skips,
  seven selftest skips and two gate skips for named missing prerequisites.

This resolves the pending CI verifier evidence for the committed head, not for
the additional working-tree changes below. No local full suite, verifier or
shard was launched. The remote PR body remains unchanged because its update was
rejected: `MCP tool call requires approval, but approval policy is never`.
Attempts to publish the blocker comment and `needs-human` label were rejected
with the same response.

The existing sequential regression now additionally carries each fresh tick
through `pacing_usage.integrate(record=True)`, the persisted `capacity.json`,
`pacing_usage.report`, and the timestamped capacity ledger. It checks both Codex
and Claude, zero and two resets, all sixteen fresh observations, exact sample
spans and the routed weights in both the report and ledger. The collected test
count is unchanged. This is offline fixture coverage; it supplies no runtime
acceptance evidence.

A direct smoke using those production integration/report functions and temporary
state passed on the selected Python 3.14.10 interpreter:

```text
resets=0: 16 persisted ticks, both pools, report and ledger assertions passed
resets=2: 16 persisted ticks, both pools, report and ledger assertions passed
Offline production smoke passed; no live-runtime or pytest-suite claim.
```

Python syntax parsing and `git diff --check` passed. The targeted pytest command,
Black formatting command and required repository-wide Black check were attempted
again and failed with `No module named pytest` / `No module named black`.
Package installation, including an explicit PyPI index attempt, failed with
`No matching distribution found for pytest`. No cached installations were found.
The new Python changes must remain uncommitted until Black can run and pass;
the new regression also still needs its actual pytest run and subsequent CI.

The installed mirror and handoff directory are both absent at this timestamp.
Post-sync live report acceptance and the repair-bound 48-hour ledger window
remain unverified. No further runtime checkbox can be completed on this runner.
