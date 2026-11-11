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
