**NEEDS-FIX — one P2 remains at exact `16d3b77463b6e823855724906a625d69338256c2`.** No P0/P1 found. Source reconciliation is not yet complete enough to establish preservation of every valid #661 payload.

**P2: explicit DB environment paths containing `~` still split learner and exporter.** [feedback.py:78](/Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__663__opener/src/feedback.py:78) bypasses resolver normalization whenever `ORCH_FEEDBACK_DB` is set. The learner’s `_conn()` uses that raw path, while the exporter expands it through `resolve_db_path()`.

I reproduced the production path-selection statements in memory, without importing application modules or opening SQLite:

| Configuration | Frozen #661 | Current repair |
|---|---|---|
| `HOME=/isolated-home`, `ORCH_FEEDBACK_DB='~/private/orchestrator.db'` | Both select `/isolated-home/private/orchestrator.db` | Learner selects literal `~/private/orchestrator.db` relative to its working directory; exporter selects `/isolated-home/private/orchestrator.db` |

This is missing valid #661 behavior: its learner initialization expanded the explicit environment path. The consequence is another wrong-store export or empty-source refusal despite learner updates elsewhere. Preserve legacy `/tmp` spelling while expanding user-home syntax, and cover this configuration with an isolated learner/CLI regression. **Confidence: high in the conditional divergence; actual live use of this configuration is unknown.**

The remaining reconciliation is supported:

| Payload path | Assessment |
|---|---|
| `.verify-floor.json` | Collected/passed expectations increase exactly seven, `3942 → 3949`; all ceilings unchanged. |
| `ARCHITECTURE.md` | Original contract restored; shared-binding claim remains overbroad because of the P2. |
| `README.md` | Same qualification. |
| `agents/codex-660.md` | Missing bootstrap marker contains no substantive behavior or acceptance proof. |
| `docs/ROUTE_WEIGHTS_EXPORT.md` | Output-only CLI and no-create/no-migrate contract restored; binding qualification remains. |
| `orchestrate.sh` | Full combined refusal log, publication switch and failure marking retained. |
| `src/feedback.py` | State-only learner binding repaired; explicit-path expansion incomplete. |
| `src/route_weights_export.py` | Indefinite partial-loss refusal restored; stronger absolute refusal, fetched comparison, URI read-only access, provenance, heartbeat ordering and commit identity retained. |
| `tests/test_firing_regressions_section.py` | Compatible mocks and repeated-invocation heartbeat coverage retained. |
| `tests/test_route_weights_export.py` | Valid guard/publication coverage retained; changed expectations restore original policy. |
| `tests/test_route_weights_export_cli.js` | Eight process-level controls retained unchanged. |
| `tests/test_route_weights_publication.py` | Capacity/no-op and URI/read-only coverage retained unchanged. |
| `tests/test_route_weights_publish_cadence.py` | Cadence wiring retained; combined-log refusal harness exists in export tests. |

I checked all **52 path bindings** across the four source states, **80 sanitized raw-file hashes**, and **10 validation source hashes**: no mismatches. The [original #660 criterion](https://github.com/stranske/Orchestrator/issues/660) requires explicit regression override and leaves full verification plus postmerge publication acceptance outstanding.

The actual XML supports **3949 cases: 3901 passed, 24 failed, 24 skipped**. The seven added cases are present. The admitted-source subset has exactly the same 24 failing identities, plus the passing private-path compatibility control; this is **not baseline full-suite PASS**. Archived logs also reproduce the three failing selftests.

Both final mutation sequences contain the named causal RED and restored GREEN: unconditional guard bypass reaches the provisioning tripwire; learner-binding disconnection fails the writer/reader equality assertion. Restoration hashes match current production source. Compatibility evidence records **111 passed plus eight subtests**; Node evidence records eight passed.

Limitations: I inspected archived execution evidence and performed read-only source/hash/XML checks; I did not rerun mutation or publication tests. Stderr-only downstream consumption remains unestablished. Hosted current-head PASS, mirror propagation, live publication with positive version and at least two implement rankings, and source acceptance remain open. **This assessment grants no merge or sibling-closure authority.**
