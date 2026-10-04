# PR 409 ship-gate review follow-through

Reviewed baseline: `bf0d50c166c88098e830b5c89c66c08ba86ab927`.

CodeRabbit findings 4178515514/4178515519 are adopted. Promotion preloading catches invalid JSON/schema/shape per experiment, preserves the file and records its error, reconciles readable states, and blocks launches for that run. Only synth_running, synth_complete and synth_verified are active synthesis phases; candidate_ready, delegated_or_pr and merged continue delivery reconciliation without extending a synthesis hold.

Findings 4178515523/4178515527 are adopted as coverage: real new-evaluation followup runs verify open/held launch counts and persisted phases; the actual CLI main branch emits a single summary line or default JSON.

## Validation

- Before product repair, `python3 -m pytest tests/test_followup_ship_gate_latch.py -q`: **9 failed, 11 passed**. Three invalid-payload cases abort preloading; six delivery-phase/order cases fail to launch. Newly evaluated and CLI coverage already pass on baseline. The evaluation fixture explicitly enables ORCH_RESEARCH_ARM within isolated test state; its initial deferred fixture run is not defect evidence.
- After product repair, same command: **20 passed**.
- `python3 -m pytest tests/test_followup_ship_gate_latch.py tests/test_synthesis_promotion.py tests/test_research_usage_guard.py tests/test_experiment_arm_identity.py -q`: **68 passed**, two existing tarfile deprecation warnings.
- `python3 src/exp_abcd.py --selftest`: **PASS**.
- `python3 -m mypy src/exp_abcd.py`: **PASS** (existing annotation-unchecked notes only).
- Black, Ruff, Bash syntax and `git diff --check`: **PASS**.
- `python3 -m pytest --collect-only -q`: **1441 collected**. Floor 1429 -> 1441; all skip/exemption ceilings unchanged.

CI owns full exact-head pytest/selftest/gate verification per CLAUDE.md. No full local verifier, installed mirror deployment, completed reviewer reassessment or merge is claimed. The normal exact-head review/thread/check and seven-minute gates remain required after push.
