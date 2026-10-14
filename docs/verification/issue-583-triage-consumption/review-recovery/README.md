# Integrated review recovery

Preserves keeper0daf94c and integrates merged runtime-AC PR586/mainad61e84. Conflicts were limited to the exact test floor and the SVG description; the merged diagram retains both descriptions and every main skip/exemption ceiling is unchanged.

Discussion4267064949: preserve the issue’s first-subsequent-run contract. An incomplete first run or invalid source role is pending before48h, then explicitly unresolved without any accepted edge. Terminal rows leave the processing limit so later completed consumptions are reached. Reports include unresolved separately from open/followed/not-followed. This does not silently substitute a later completed run for the first run.

Discussion4267064954: file bytes are fsynced before replace, then the parent directory is fsynced and its descriptor reliably closed. Keeper’s five close/failure cleanup regressions remain intact.

`python3 -m pytest tests/test_triage_shadow.py tests/test_runtime_ac_shadow.py tests/test_value_chain_monitor.py tests/test_orchestrator_loop_svg_wiring.py -q`:105passed. Four touched-module selftests, Black/Ruff/focusedmypy PASS. Exact collection3329 = main3306 +14initial +5keeper +4review nodes. This executed-node union floor does not claim full-suite PASS; hosted current-head checkout/flat-mirror verification remains required.

Remove unresolved expiry: the100-row starvation/invalid-source regressions assert FAIL inall3cases ->byte-identical restore3PASS. Remove directory fsync: ordering assertionFAIL ->byte-identical restore1PASS. `recovery-mutation-proof.json` binds restored production sources. Initial ranking/accepted-edge mutation evidence in the parent directory remains attributed to761807b, not this later source revision.

Earlier private761807b mirror run was NOT VERIFIED:3285passed/3Gemini advertised-model failures/21expectedmirror skips;104selftests and5gates passed,122mypy modules checked/0exempt. Allthree failures independently reproduce on unchangedmain34bfcff. Existing runtime issue578 owns catalog recovery; no pin/effort/flag or deployment change was made. That earlier full run is not a current-head result.

Final trivial review4267121941: the existing failure-stage test now also raises before NamedTemporaryFile returns, proving previous snapshot preservation, propagated OSError and successful later recovery.106affected testsPASS;3330exactcollection(main3306+24). Production source is unchanged by this final test-only addition.
