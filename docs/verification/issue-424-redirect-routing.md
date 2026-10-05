# Issue #424: redirect routing and provenance

Dedup: sweep intake, RedirectAgent, dry-run plans and role-run metadata already exist. This change extends their current boundaries rather than adding a dispatcher or another decision path.

An unset or `auto` sweep backend lets the role router choose the judgment backend. Explicit backends retain precedence. A redirect/decompose proposal that supplies no worker uses the task router with reserve and backup seats excluded. `implement` is a task prior rather than a role name, so the worker selection calls the existing `router.select_agent` rail. The plan records the worker's source. A missing worker yields an error and inspection commands; plan construction itself refuses missing/placeholder agents. Watch preserves the policy recommendation while withholding apply commands. Existing apply actions, authority checks, corpus flag and manual publication boundary remain unchanged.

Brain role-run metadata now retains source and report state from the sweep, normal dispatch and historical replay. Three synthetic fixture reports cover auth failure, an exited attempt and scope drift without live paths or model calls.

Validation:

- `python3 -m pytest tests/test_redirect_chain_backend_and_agent.py tests/test_roles_lineage.py tests/test_redirect_apply_prescreen.py -q`: 50 passed.
- New acceptance file: 27 cases; measured whole-tree collection: 1912, previous 1885. All skip/exemption ceilings retained.
- Actual cursor-default mutation: 4 failed, restore: 4 passed.
- Actual missing-agent guard mutation: 8 failed, restore: 8 passed.
- Restoring both missing-worker and placeholder behavior: 14 failed across the two required acceptance tests; byte-identical restoration.
- Removing persisted source/state: 3 failed, restore: 3 passed.
- Black, Ruff, touched-file mypy and shell syntax checks passed.

Full `python3 src/verify.py`: 1912 passed, zero skips, 99/99 selftests, 5/5 gates, and 116 checked mypy modules with zero exemptions. Two legacy selftests were updated to assert constructor refusal and inspection-only classification; the existing apply-time malformed-plan guard remains tested. The exact pushed head is recorded in the PR and relocated lane state. CI supplies the exact-head source and flat-mirror verdict. Passing source tests do not establish installed runtime behavior; reviewed mirror publication remains manual.

The first CI run exposed two existing lineage tests relying on installed worker capacity. With `roles.router.load_capacity` forced to an empty agent map, the unchanged lineage suite reproduces both failures. Its private feedback fixture now supplies an explicit available Codex worker to the real router. The same empty-capacity outer environment then passes all 50 focused cases; this isolates test prerequisites without changing production routing or relaxing any skip ceiling. CI must validate the repaired head independently.
