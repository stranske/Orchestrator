# Issue 599: keepalive round 11 acceptance evidence

Check date: 2026-11-11 (America/Chicago). Tested implementation: `f016777027d5c141188717719dae54248cf558cd`.

Outcome: all 41 source-issue rows resolve; zero reviewed missing consumers.
The command `/opt/anaconda3/bin/python3 src/capability_admission.py` exited 0
on a fresh byte-for-byte copy of `/Users/teacher/.codex/orchestrator/capabilities.json`.
`ORCH_CAPABILITIES_PATH` selected that copy so the adjacent ledger lock was writable.
The command used the real runtime for its other inputs. This is a copied-ledger
observation, not proof that these declarations have been deployed onto the live ledger.

```text
  CONSUMERS (10th): 51 of 51 declare a consumer this tree can find — acting (capability-level) 9, in grace 0, not invoked 0, exercise-bound 21, unmeasured 0
                   not acting (trailing window) 21 (blocking 0, pre-cutoff debt 21); prose 0, missing 0, undeclared 0 — DRAINABLE 0 (reviewed absent 0, unresolved 0; reviewed absence remains finding debt)
```

The focused command was `/opt/anaconda3/bin/python3 -m pytest -q
 tests/test_capability_admission.py tests/test_capability_admission_standalone.py`
(with `PYTHONPATH=src` and disposable `ORCH_STATE_DIR`, `ORCH_LOCAL_RUNTIME`,
and `ORCH_CAPABILITIES_PATH`). Result: 44 passed, 3 environment skips in 10.28s.
The named declaration test failed after replacing `route-weights-export`'s consumer
with prose (exit 1, identifying `KNOWN_GATES:route-weights-export`), then passed
on byte-identical source restoration (exit 0, 1.65s). No lifecycle or action credit
was changed. The 21 rows with no trailing-window action remain separately reported.

## CI failure and bounded drain

The failing `test-quality` job in Gate run `43124791570` stopped at
`Apply runtime acceptance-criteria label`: GitHub returned HTTP 403,
`Resource not accessible by integration`. It was not a failing Python assertion.
Blocking/drainable pair: label write denied / apply the required label using the
available authenticated account and rerun the failed jobs. The label was applied;
`test-quality` passed on attempt 2. No workflow permissions or pins were edited.
The general workflow label-permission defect is follow-up work in the workflow
source repository, outside this declaration repair.

## Declaration inventory

These are output-consumer bindings, not a claim of measured invocation. The older
`issue-599-consumer-bindings.md` records historical snapshots, including the old
missing docs-drift finding. The current declaration names its explicit apply-mode
handover, `scripts/docs_drift_fix_agent.py:apply_issues`; it files the proposed issues.
No current source-issue row is marked `consumer_missing: true`.

| Source issue row | Current downstream consumer |
| --- | --- |
| `abcd-experiment` | `synthesis_promotion.py:ensure_evaluated_state; objective_anchor.py:anchor_experiment` |
| `adversarial-review` | `adversarial.py:file_followup consumes the panel verdict after merge` |
| `agy-runtime-isolation` | `dispatcher.py:plan_dispatch; dispatcher.py:offload use the confined Gemini command` |
| `capability-activation-audit` | `verify.py:run_gates fails verification when the activation audit exits nonzero` |
| `capability-admission-gate` | `verify.py:run_gates fails the verify run on a blocking row; capability_admission.py:format_report prints it` |
| `capability-firing-monitor` | `switch_review.py:firing_regressions consumes persisted findings` |
| `capability-propensity` | `capability_advisor.py:advise ranks the offered candidates` |
| `codemod-campaign` | `codemod_lane.py:build_delegation_prompt consumes the campaign and dry-run plan to produce the worker prompt` |
| `coverage-testgen-trigger` | `testgen_lane.py:ranked_sources; testgen_gate.py:main` |
| `cross-repo-coordination` | `cross_repo_lane.py:build_dry_run_plan consumes the coordination proposal to produce work items and dispatch prompts for manual release` |
| `deliberate-break-verifier` | `runtime_ac.py:evaluate_results consumes deliberate-break JSON; local_verify.py:record_verdict records the run verdict` |
| `docs-drift-fix-agent` | `scripts/docs_drift_fix_agent.py:apply_issues hands repair batches to GitHub as issues on Maint 87's dispatch-only apply path` |
| `epic-decomposition` | `epic_lane.py:build_dispatch_prompts consumes the validated DAG to produce worker prompts` |
| `evidence-acquisition` | `evidence_acquisition.py:render_tick_line reads the emitted acquisition plan` |
| `feature-reflection-cli` | `periodic_report.py:format_human consumes the feature registry summary` |
| `feature-scan` | `feature_scan.py:apply_scan writes the scan's unlogged structures to the registry on --apply` |
| `feedback-store` | `router.py:learned_ranks; periodic_report.py:build_report` |
| `frontend-verifier` | `runtime_ac.py:_completed_result consumes frontend verifier exit status and JSON; runtime_ac.py:evaluate_results fails the runtime gate on failed checks` |
| `live-keepalive-supervisor` | `keepalive_supervisor.py:close_verified_issues; redirect_shadow.py:record_redirect; periodic_report.py:format_human` |
| `local-model-profile-trial` | `periodic_report.py:build_report; observability_dashboard.py:build_dashboard` |
| `offload` | `roles.py:run_prompt_agent; roles.py:run_adjudicator_agent consume backend output; feedback.py:record_role_run binds the consuming role to its backend run` |
| `partitioned-review` | `partitioned_review.py:synthesize_results validates and combines partition outputs` |
| `rail-exercise-cadence` | `periodic_report.py:_rail_exercise_summary reads the cadence artifact` |
| `range-lane-rollout` | `range_lane_rollout.py:main delegates only on the triple-guarded apply branch` |
| `redirect-plan` | `redirect_apply.py:apply_candidates consumes the recovery plan` |
| `redirect-policy` | `redirect_plan.py:plan consumes the policy decision` |
| `repo-playbook` | `dispatcher.py:build_prompt; capability_advisor.py:advise` |
| `research-scheduler` | `tick.py:research_tick launches the planned experiments` |
| `research-usage-guard` | `exp_abcd.py:followup consumes the opportunity decision before a follow-up` |
| `role-adjudicator` | `switch_review.py:format_report consumes agreement and cost summaries` |
| `role-decomposer` | `epic_lane.py:build_dispatch_prompts validates the role DAG before worker prompts` |
| `role-triage` | `triage_shadow.py:consume orders candidates only behind ORCH_TRIAGE_CONSUME; switch_review.py:format_report reports consumption` |
| `route-weights-export` | `route_weights_export.py:publish_document hands the document to the Workflows config repository` |
| `runtime-ac-checks` | `merge_guard.py:evaluate_merge_gate consumes the runtime gate verdict` |
| `stall-watcher` | `redirect_policy.py:decide consumes the classified stall` |
| `strategy-experiments` | `exp_abcd.py:prepare_arms launches the strategy's planned arms` |
| `switch-review` | `switch_review.py:raise_questions; switch_review.py:format_report` |
| `synthesis-promotion` | `synthesis_promotion.py:default_delivery_discovery links an external delivery; synthesis_promotion.py:default_mirror_outcome mirrors its measured outcome` |
| `testgen-lane` | `testgen_gate.py:main consumes candidate tests for the acceptance gate` |
| `thompson-hybrid-routing` | `router.py:select_agent dispatches the sampled challenger` |
| `windowed-capacity-policy` | `router.py:select_agent; range_lane_rollout.py:build_rollout; rate_incidents.py:record_lane_round` |
