# Issue 1214 acceptance record

Check date: 2026-11-11T19:03:30.836849+00:00. Baseline: 7a0e09a4.

Outcome 1: real GitHub rate-limit JSON printed by dispatcher Codex assess, with recorded sandbox selection and matching terminal capacity-ledger row. UNKNOWN until all three are observed.

Exact live command (post-merge observation is deferred by the issue's final narrowed scope):

```sh
/opt/anaconda3/bin/python3 src/dispatcher.py offload --agent codex --mode assess --cwd /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__1214__opener --timeout 120 --subject 'stranske/Orchestrator#1214' --prompt 'Do not create, modify or delete any file; print your answer to stdout. Run gh api rate_limit and print the returned JSON.'
```

Stop rule: at most three repair attempts or 45 minutes from this record, whichever occurs first. A live assess command has a 120-second provider bound. Stop at the first unresolved failing step and report it; do not alter unrelated controls.

Current main already contains adapters.build_command sandbox selection and permission restrictions, the generic capacity-ledger writer, and switch_review interval reporting. Recover only dispatcher probe/fallback and tests from ancestor a459028c, adapting to current main.

Validation is limited to `/opt/anaconda3/bin/python3 -m pytest tests/test_dispatcher_offload.py -k sandbox`, including the required availability mutation and restored run. `src/verify.py` is not run: it launches the full suite, prohibited by the current owner instruction. No full-suite pass is claimed.

The post-merge live transcript/ledger and actual seven-day report are UNVERIFIED and deferred as the issue explicitly directs. No adapters or switch_review changes are planned.

## Observed implementation evidence

- `sandbox-pass.txt`: 30 collected, 30 passed; pytest 1.01 seconds, total process 2.140 seconds.
- Deliberate mutation: insert `return True` as the first statement of `_sandbox_has_network`, then execute the same sandbox command. `sandbox-deliberate-break.txt` records exit 1 and the named `test_sandbox_network_failure_falls_back_to_workspace_write` assertion: `workspace-write and the fallback preamble are absent`. Other probe/fallback tests also fail as expected under this global mutation.
- The source was restored in a `finally` block before rerunning. `sandbox-restored.txt`: 30 collected, 30 passed; pytest 0.90 seconds, total process 1.895 seconds. The deliberate break is absent from the delivered source.
- Mocked transport tests use the actual adapter-generated argv and the existing ledger writer with private paths. They cover timeout/DNS/connection fallback, authentication/API rejection without fallback, unknown/unstarted probe without widening, managed permissions in the probe, read-only profile refusal, preserved allowed write roots/model, non-assess/non-Codex unchanged behavior, prompt/diagnostic text and start/complete ledger persistence, including failed fallback execution.
- Mock-clock tests prove setup reduces the five-second allowance, an exhausted allowance launches no process, and a late successful return is classified as timeout.
- `sandbox-harness-failure.txt` retains the initial expanded-test failure: the non-assess Codex mock returned plain text instead of the JSON stream that path requires. Only the mock response was corrected.

## Live limits and follow-up

`baseline-live.txt` records exit 1 after 0.323 seconds: `PermissionError` creating the dispatcher log under `/Users/teacher/.codex/handoff/dispatch-logs`. No provider launched, no terminal ledger row or real rate-limit data was produced. No permission/settings changes or shared-path relocation were made. Outcome 1 remains BLOCKED/UNVERIFIED. A future authorized runtime with its normal dispatcher-log access must execute the recorded command after merge, retain the selected sandbox/terminal ledger row, then observe the actual seven days after that merge using the existing switch_review reporter. This PR neither merges itself nor claims that future interval.

The recovered ancestor's empty custom `read-only` permission table and explicit disk-read override were replaced with the built-in `:read-only` profile plus `--include-managed-config`, without permission overrides. Official references: [permission profiles](https://learn.chatgpt.com/docs/permissions) and [sandbox CLI flags](https://learn.chatgpt.com/docs/cli/reference#codex-sandbox). These establish the argument design; they do not substitute for the deferred live execution. The fallback no-write preamble is a behavioral instruction; workspace-write remains technically capable of writing within the permitted roots, as the issue requests.

No additional issues were opened: the issue's final narrowed scope already explicitly defers live API evidence and the seven-day observation. `src/verify.py` and the full suite remain NOT RUN under the current owner restriction.

## Keepalive round 2

Check date is 2026-11-11T19:30:44Z. Outcome 1 remains unverified. Success requires the dispatcher to return real GitHub rate-limit JSON, report the selected sandbox, and persist a matching successful terminal ledger row. Run from the repository root on the merged revision in the normal dispatcher runtime.

```sh
/opt/anaconda3/bin/python3 src/dispatcher.py offload --agent codex --mode assess --cwd "$PWD" --timeout 120 --subject 'stranske/Orchestrator#1214' --prompt 'Do not create, modify or delete any file; print your answer to stdout. Run gh api rate_limit and print the returned JSON.'
```

Stop this repair after two attempts or at 2026-11-11T19:45:44Z, whichever comes first. The live command uses a 120-second provider timeout. Record the UTC start, result, sandbox, and ledger row sharing the result's run ID. After seven days have elapsed, use the actual merge timestamp as `--since` in the command below and retain both counts.

```sh
/opt/anaconda3/bin/python3 src/switch_review.py --assess-sandbox-fallback --since "$MERGE_TIMESTAMP" --json
```

## Keepalive inspection — 2026-11-11

- [x] Inspect dispatcher, adapter command generation, existing capacity-ledger writer,
  switch-review interval report, and sandbox tests before assigning changes.

Inspection baseline: checked-out `f410e500`, with local `origin/main` at `4551cd69`.
This is a local-ref reconciliation, not a claim that a network fetch succeeded.
`adapters.py` and `switch_review.py` have no diff against that main ref. The branch
already implements `_sandbox_has_network`, the five-second shared probe clock,
DNS/connection/timeout versus authentication/API classification, adapter-built
fallback arguments, the no-write preamble, conditional diagnostic, restricted
profile refusal, and `sandbox_fallback=true` on start and terminal ledger rows.
`adapters.record_ledger` is the existing NDJSON writer; no parallel writer is needed.
`switch_review.assess_sandbox_fallback_report` already accepts an explicit start,
prints both counts, and leaves incomplete observation windows unverified.

The missing regression identified here is a write root becoming protected during
the network probe. `test_sandbox_fallback_rechecks_protected_write_roots` exercises
the real adapter's refusal when rebuilding the workspace-write command, and checks
that no provider, dispatch log, run ID, ledger row, or fallback-success diagnostic
is produced. No dispatcher, adapter, or reporting implementation changes were made.

A direct standard-library smoke check of that scenario exited 0 and printed:

```text
PASS: real adapter refuses a newly protected write root after a DNS probe failure
PASS: no provider launch, run ID, dispatch log, ledger row or fallback-success diagnostic
```

The new pytest test remains unverified by pytest. This runner's CI-selected absolute
interpreter is `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3` (CI selects 3.14;
`/opt/anaconda3/bin/python3` is absent). It lacks both pytest and Black. Installing
them failed because `pypi.org` could not resolve; the local wheel cache also lacks
both packages. The attempted sandbox command failed with `No module named pytest`.
Both Black formatting and the required repository-wide Black check failed with
`No module named black`. Python syntax compilation and `git diff --check` passed.
No Python commit is permitted until the required Black gate can run successfully.
The pre-existing green/mutation receipts above do not validate this new test.
`src/verify.py` was also launched with that absolute interpreter as requested in
this task. It emitted no result while running its module self-tests and was
interrupted after more than six minutes (exit 130); this is incomplete verification,
not a passing result. `keepalive-validation-blocked.txt` retains this round's
validation outcomes. No acceptance checkbox was marked complete for these runs.

The live assess outcome and the actual seven days after implementation merge remain
UNVERIFIED. No live provider command, merge, or synthetic observation interval was
used to substitute for that evidence.

## Explicit interval follow-up — 2026-11-11

The first unchecked task now has source and regression-test changes pending in the
working tree. `switch_review.py --assess-sandbox-fallback` accepts `--since` and an
optional exclusive `--until`, reports both UTC bounds beside the two required
counts, rejects invalid intervals as usage errors, and excludes events at the
end boundary or after the observation clock. Omitting `--until` retains the
seven-day default. Incomplete windows remain unverified. The existing two sandbox
reporting tests now exercise these cases without changing the collection count.

Direct standard-library assertions against temporary ledger fixtures passed for
boundary exclusion, start/complete deduplication, both counts, future-row exclusion,
incomplete windows, invalid intervals, ISO offsets, epoch zero, and the default
seven-day interval. One observed fixture report was:

```text
assess sandbox fallback (3d) [2026-11-06T00:00:00Z, 2026-11-09T00:00:00Z): fallback_runs 2; network-caused assess incompletions 2
```

This is synthetic fixture verification, not live post-merge evidence. Syntax
compilation of both changed Python files and `git diff --check` also passed.
`switch-review-interval-validation.txt` retains the attempted required commands:
pytest and both Black commands failed because their modules are absent; installing
them failed because `pypi.org` could not resolve. The verify command was stopped
after a 45-second diagnostic bound without a result and remains incomplete.
The mandatory Black gate therefore prohibits a Python commit. Source and test
changes remain uncommitted, and no task or acceptance checkbox is marked complete.
The live assess and actual seven-day post-merge observation remain UNVERIFIED.

## Real subprocess deadline follow-up — 2026-11-11T20:34Z

The first unchecked criterion remains blocked. The existing timeout test now
launches a real Python child that sleeps for 30 seconds and checks that the
production five-second subprocess deadline stops it. It also checks that exactly
one subprocess is launched with a positive timeout no greater than five seconds.
The collection count is unchanged. The ten-second assertion allows process startup
and cleanup overhead; the subprocess timeout must still be at most five seconds.

`real-probe-deadline-validation.txt` retains this round's commands and outputs.
A standard-library smoke check against the production probe stopped the real
child after 5.006 seconds with `classification=timeout` and `has_network=false`.
This is not a pytest result. Python syntax parsing and `git diff --check` pass.

The CI-selected absolute interpreter remains
`/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`. The required sandbox pytest
command exits 1 because pytest is absent. Both Black commands exit 1 because Black
is absent. Installing the packages into `/tmp` from PyPI fails because `pypi.org`
cannot resolve. `src/verify.py` produced no result before a 60-second diagnostic
bound and was terminated, so full verification remains incomplete. The mandatory
Black gate prohibits committing the Python change. No commit or push was made.

The live PR lookup confirms #1355 is open, ready for review, and unmerged. Its
checked post-merge observation task is unsupported: the live assess evidence and
the actual seven days after merge are still UNVERIFIED. Attempts to uncheck that
task, add `needs-human`, and post the blocker comment were rejected by the tools
because they require approval and this run's approval policy is `never`.
No acceptance checkbox was completed.
