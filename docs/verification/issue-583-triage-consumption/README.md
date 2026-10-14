# Issue 583: ranking consumption proof

This extends `triage_shadow` and the existing Brain consumption API. Atomic ranking publication requires a valid live proposal. Consumption defaults off, requires 20 valid live cycles and a ranking no older than 7200 seconds, keeps all caller-filtered candidates, refuses disjoint rankings to prevent crediting a new rule pick, and records no worker action. Linkage credits the first subsequent matching non-role run only after its completion event; no such run after 48 hours is recorded not-followed, without an edge.

`python3 -m pytest tests/test_triage_shadow.py tests/test_value_chain_monitor.py tests/test_orchestrator_loop_svg_wiring.py -q`: 59 passed. Touched triage, outcomes, capabilities and value-chain module selftests pass. Black, Ruff and focused mypy pass. Exact collection is 3309 (main3295 +14), all skip and exemption ceilings unchanged. This union floor is not a full-suite PASS claim. Current-head hosted checkout and flat-mirror verification remain required.

Both deliberate mutations ran `python3 -m pytest tests/test_triage_shadow.py::test_consumed_ranking_records_an_accepted_edge_when_the_opener_follows -q`:

- Preserve input order after eligibility checks: assertion FAIL (first target remains rule pick instead of triage pick). Restore: PASS.
- Remove `feedback.record_capability_consumption` from the linker: assertion FAIL (no accepted edge). Restore: PASS.

`mutation-proof.json` binds the byte-identical restored production sources; paired raw pytest logs preserve both failures and successful restores. All role/worker runs and edges in these tests are isolated fixtures. No provider invocation, production consumption credit, live worker action, flag activation, or mirror deployment is claimed.

Future acceptance stays with Capability Program #575: after merge and the manual sync, wire the external opener invocation, then test a bounded flag-on window. Check actual followed edges on 2026-10-20 and outcomes on 2026-11-03. This PR does not change the external lane prompt or any script under ~/.codex/bin.
