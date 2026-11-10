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

## Current-head reconciliation — 2026-11-10T06:23:17Z

Reviewed commits `0e443b22`, `2a6fb731` and `e6edc543`. The two checked
implementation/mutation tasks remain supported. CI now verifies committed head
`e6edc543f0cc73a23f21f57f98ad01e0117b951d`, testing merge revision
`11073416d5e248920cdfef30844fda3045bf2efa`, in run
[42767318057](https://github.com/stranske/Orchestrator/actions/runs/42767318057):

- [Checkout aggregate](https://github.com/stranske/Orchestrator/actions/runs/42767318057/job/129667557195)
  succeeded at `06:21:37Z`: 8,451 passed, zero failed, 8,480 collected;
  104 selftests spoke, 3/5 gates green; 27 test, seven selftest and two gate
  skips for named missing prerequisites.
- [Mirror aggregate](https://github.com/stranske/Orchestrator/actions/runs/42767318057/job/129667913866)
  succeeded at `06:20:38Z`: 8,430 passed; 104 selftests spoke, 3/5 gates
  green; 48 test, seven selftest and two gate skips for named prerequisites.

This supplies the pending verifier evidence for the committed head on CI's
Python 3.14.10. It does not verify the new working-tree extension below. The
historical focused suite used Python 3.12.2 on the original machine. No local
full suite, shard or verifier was launched.

The working-tree extension checks delayed report evidence at every fresh tick:
recompute sixty seconds after the persisted tick, read the prior snapshot from
disk, retain its routed weight/timestamp/age in the report, and display the
distinct recomputed weight. At the first observation these are respectively
0.75 and 0.5, with a displayed delta of -0.250. Capacity, usage samples and
ledger must remain byte-identical throughout report generation. This extends
the existing test without changing collection counts and supports interpreting
the requested runtime evidence; it does not establish live acceptance.

A direct smoke of the production functions with temporary state passed on the
selected `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`:

```text
resets=0: 16 delayed reports, both pools; tick identity, recomputed weights and byte-identical evidence passed
resets=2: 16 delayed reports, both pools; tick identity, recomputed weights and byte-identical evidence passed
Production smoke passed; pytest and Black remain unavailable.
```

Python AST parsing and `git diff --check` passed. The focused pytest command
with `-m 'not slow'`, Black formatting and the prescribed repository-wide Black
check were attempted and failed with `No module named pytest` / `No module
named black`. Temporary dependency installation also failed. The Python edit
is intentionally uncommitted: the mandatory pre-commit Black gate cannot pass.
Run the actual focused suite and both Black commands before committing, then
obtain CI for the resulting revision.

The PR body update, blocker comment and `needs-human` label each returned
`MCP tool call requires approval, but approval policy is never`. No remote
checkbox or label was changed, and no alternate mutation path was attempted.
The verifier checkbox may be reconciled for `e6edc543`, but the pending source
edit needs its own verification. Runtime acceptance stays unchecked: the local
mirror and handoff directory are absent, and there is no repair-bound live
report or clean 48-hour ledger window. PR #1312 is open and ready for review.
