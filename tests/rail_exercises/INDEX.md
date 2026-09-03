# Rail exercise contract index

All source contracts were ported mechanically. `$CONTRACT_DIR` resolves to the directory containing `contract.json`; `$REPO_ROOT` resolves to this repository root at run time.

| Capability | Round | Proposer | Contract | Port note |
|---|---|---|---|---|
| agy-runtime-isolation | r15 | P0-coord | tests/rail_exercises/agy-runtime-isolation/arm-a/contract.json |  |
| capability:reference-sync-hygiene-test-gate | r15 | P0-coord | tests/rail_exercises/capability_reference-sync-hygiene-test-gate/arm-a/contract.json |  |
| capability-admission-gate | r15 | P1-codex | tests/rail_exercises/capability-admission-gate/arm-a/contract.json |  |
| epic-decomposition | r15 | P1-codex | tests/rail_exercises/epic-decomposition/arm-a/contract.json |  |
| live-keepalive-supervisor | r15 | P1-codex | tests/rail_exercises/live-keepalive-supervisor/arm-a/contract.json |  |
| redirect-apply-bootstrap | r15 | P1-codex | tests/rail_exercises/redirect-apply-bootstrap/arm-a/contract.json |  |
| redirect-plan | r15 | P1-codex | tests/rail_exercises/redirect-plan/arm-a/contract.json |  |
| redirect-policy | r15 | P1-codex | tests/rail_exercises/redirect-policy/arm-a/contract.json |  |
| stall-watcher | r15 | P1-codex | tests/rail_exercises/stall-watcher/arm-a/contract.json |  |
| completion-event-lineage | r15 | P2-gemini | tests/rail_exercises/completion-event-lineage/arm-a/contract.json |  |
| coverage-testgen-trigger | r15 | P2-gemini | tests/rail_exercises/coverage-testgen-trigger/arm-a/contract.json |  |
| evidence-acquisition | r15 | P2-gemini | tests/rail_exercises/evidence-acquisition/arm-a/contract.json |  |
| feature-reflection-cli | r15 | P2-gemini | tests/rail_exercises/feature-reflection-cli/arm-a/contract.json |  |
| feedback-store | r15 | P2-gemini | tests/rail_exercises/feedback-store/arm-a/contract.json |  |
| issue-readiness | r15 | P2-gemini | tests/rail_exercises/issue-readiness/arm-a/contract.json |  |
| docs-drift-fix-agent | r15 | P3-cursor | tests/rail_exercises/docs-drift-fix-agent/arm-a/contract.json |  |
| local-model-profile-trial | r15 | P3-cursor | tests/rail_exercises/local-model-profile-trial/arm-a/contract.json |  |
| research-scheduler | r15 | P3-cursor | tests/rail_exercises/research-scheduler/arm-a/contract.json |  |
| research-usage-guard | r15 | P3-cursor | tests/rail_exercises/research-usage-guard/arm-a/contract.json |  |
| strategy-experiments | r15 | P3-cursor | tests/rail_exercises/strategy-experiments/arm-a/contract.json |  |
| synthesis-promotion | r15 | P3-cursor | tests/rail_exercises/synthesis-promotion/arm-a/contract.json |  |
| agy-runtime-isolation | r16 | P2-second-arms | tests/rail_exercises/agy-runtime-isolation/arm-b/contract.json |  |
| capability-admission-gate | r16 | P2-second-arms | tests/rail_exercises/capability-admission-gate/arm-b/contract.json |  |
| capability:reference-sync-hygiene-test-gate | r16 | P2-second-arms | tests/rail_exercises/capability_reference-sync-hygiene-test-gate/arm-b/contract.json |  |
| completion-event-lineage | r16 | P2-second-arms | tests/rail_exercises/completion-event-lineage/arm-b/contract.json |  |
| docs-drift-fix-agent | r16 | P2-second-arms | tests/rail_exercises/docs-drift-fix-agent/arm-b/contract.json |  |
| evidence-acquisition | r16 | P2-second-arms | tests/rail_exercises/evidence-acquisition/arm-b/contract.json |  |
| feature-reflection-cli | r16 | P2-second-arms | tests/rail_exercises/feature-reflection-cli/arm-b/contract.json |  |
| feedback-store | r16 | P2-second-arms | tests/rail_exercises/feedback-store/arm-b/contract.json |  |
| issue-readiness | r16 | P2-second-arms | tests/rail_exercises/issue-readiness/arm-b/contract.json |  |
| live-keepalive-supervisor | r16 | P2-second-arms | tests/rail_exercises/live-keepalive-supervisor/arm-b/contract.json |  |
| local-model-profile-trial | r16 | P2-second-arms | tests/rail_exercises/local-model-profile-trial/arm-b/contract.json |  |
| redirect-apply-bootstrap | r16 | P2-second-arms | tests/rail_exercises/redirect-apply-bootstrap/arm-b/contract.json |  |
| research-scheduler | r16 | P2-second-arms | tests/rail_exercises/research-scheduler/arm-b/contract.json |  |
| research-usage-guard | r16 | P2-second-arms | tests/rail_exercises/research-usage-guard/arm-b/contract.json |  |
| synthesis-promotion | r16 | P2-second-arms | tests/rail_exercises/synthesis-promotion/arm-b/contract.json |  |
| coverage-testgen-trigger | r17 | P-second-arms-r17 | tests/rail_exercises/coverage-testgen-trigger/arm-b/contract.json |  |
| epic-decomposition | r17 | P-second-arms-r17 | tests/rail_exercises/epic-decomposition/arm-b/contract.json |  |
| redirect-plan | r17 | P-second-arms-r17 | tests/rail_exercises/redirect-plan/arm-b/contract.json |  |
| redirect-policy | r17 | P-second-arms-r17 | tests/rail_exercises/redirect-policy/arm-b/contract.json |  |
| stall-watcher | r17 | P-second-arms-r17 | tests/rail_exercises/stall-watcher/arm-b/contract.json |  |
| strategy-experiments | r17 | P-second-arms-r17 | tests/rail_exercises/strategy-experiments/arm-b/contract.json |  |
| capability-activation-audit | r18 | P-audit-routing | tests/rail_exercises/capability-activation-audit/contract.json |  |
| capability-firing-monitor | r18 | P-audit-routing | tests/rail_exercises/capability-firing-monitor/contract.json |  |
| feature-scan | r18 | P-audit-routing | tests/rail_exercises/feature-scan/contract.json |  |
| range-lane-rollout | r18 | P-audit-routing | tests/rail_exercises/range-lane-rollout/contract.json |  |
| switch-review | r18 | P-audit-routing | tests/rail_exercises/switch-review/contract.json | Named skip: source contract declares the requested SUSPECT/value/drainable model absent and its pass fixture deliberately returns FAIL; the assertion is retained, not softened. |
| thompson-hybrid-routing | r18 | P-audit-routing | tests/rail_exercises/thompson-hybrid-routing/contract.json |  |
| windowed-capacity-policy | r18 | P-audit-routing | tests/rail_exercises/windowed-capacity-policy/contract.json |  |

Total source contracts: 49. Contracts with multiple source arms are retained as `arm-a`, `arm-b`, etc. No contract was omitted.
