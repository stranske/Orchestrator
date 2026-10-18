# PR #689: trial launch gate fix

The launch planner refuses new producer work at the trial deadline unless the persisted verdict
is `pass`. The persisted start defines the exact 21-day window; the shell-exported end date is a
fallback before a start is persisted. One helper supplies the deadline to launch admission,
trial-cohort accounting, report days remaining, and verdict grace/unknown deadlines.

Followups and the verdict poll continue without launches. Pending unknowns resolve fail at day 42
on the next poll. Fail receipts survive the shell defaulting the producer off and the work draining.
Reports include post-window launch counts independently of the original trial cohort.

## Validation

Runtime: `/opt/anaconda3/bin/python3` throughout. No permission or privacy settings changed.

- Final producer/decoupling tests: **73 passed** (`producer-final.log.gz`).
- Required `-m pytest tests -q -k "producer or experiment or switch_review"`:
  **200 passed, 4510 deselected**, with `ORCH_AGENT_RUNTIME_DIR=/tmp/pr689-test-agent-runtime`
  (`focused-isolated.log.gz`). The initial unisolated run had 196 passed / 3 failed, all
  denied writes to the shared Vibe runtime config (`focused.log.gz`).
- Eight module selftests pass: experiment_producer, exp_abcd, synthesis_promotion, tick,
  switch_review, local_verify, research_usage_guard, capacity. The exp_abcd rerun uses
  `ORCH_AGENT_RUNTIME_DIR=/tmp/pr689-selftest-agent-runtime`; its initial run was blocked by
  that same shared Vibe config write. Final tick selftest was rerun after the drain receipt change.
- Changed-code Ruff, Black, MyPy, and `git diff --check` pass.
- `.verify-floor.json` matches main at `80336468ffa2720d508bec4a99ee7f8d507c0f06`
  byte-for-byte (blob `db60970dc544f18c86246c66fd1c61d5bb62568d`). No count or ceiling was set.

Full verifier **RED**, exit 1: **4,676 passed, 33 failed**, 319 subtests passed;
103/107 module selftests ran, with adapters, capability_propensity, exp_abcd, and tick_watchdog
failing. This is not solely the allowed ps/sandbox-exec/network exception population:

- 10 watchdog tests: denied process-table access / consequent observer timeout.
- 4 Seatbelt tests: nested `sandbox-exec` denied (`sandbox_apply: Operation not permitted`).
- 4 mirror tests: system `mktemp` writes outside approved roots denied.
- 5 evaluator/ABCD tests: shared Vibe runtime config write denied.
- 3 profile-trial tests and 1 offload test: writes outside approved roots denied.
- 4 home/private-state assertions: the runtime temporary directory lies under the live
  orchestrator root, or changes the broad-workspace refusal reason.
- 2 model-resolution tests: host Gemini authentication policy differs from the expected
  model-resolution verdict. Not independently reproduced against main; baseline status UNKNOWN.

The entrypoint diagnostic also could not acquire the shared capabilities lock. No network-only
failure is established by this run. Full failure names and per-test tracebacks are retained in
`verify.log.gz` and `verify-pytest.xml.gz`. The verifier reports floor 4,635 vs 4,709 collected;
its instruction to rewrite the floor was not applied because this round explicitly carries
main's floor byte-for-byte. No green full gate is claimed.

The broad run began before the final off-switch/drained receipt and its one regression test
were added. Final code is covered by the subsequent 73-test producer run and 200-test required
filtered run; the broad run is not represented as a clean final-head acceptance.


Logs are retained losslessly as gzip files. No CI wait, deployment, or live-trial claim.
