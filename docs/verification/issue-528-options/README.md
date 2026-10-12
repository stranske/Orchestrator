# Issue 528 option pinning evidence

Base: e626002302d025e471e9e6b95b5cbf5ff09d7141. `proof.json` records exact argv, cwd, statuses and source hashes. The only production difference from the mutant is interpreter option handling in `src/mirror_reader.py`.

The named real-publication test passes, fails five assertions after replacing production with unchanged base source (Node split/equals/short preload plus bash -o/+o), then passes after byte-identical restoration. All five failures are old-versus-new code observations, not import or setup errors. The child import checks and ordinary relative data spelling remain explicit. `production-mutant.py.txt` and `production-restored.py.txt` retain both source versions.

Focused Python: 126 passed plus 29 subtests; Node TAP: 12 passed, including all nine existing positive/negative reentry controls. Exact collection: 2963. Full local verify.py is intentionally not claimed: this parser change touches no live ledger or CI-skipped capability; per CLAUDE.md the PR verdict is hosted verify.py in checkout and flat mirror shapes. Skip ceilings are unchanged. Installed mirror publication and provider acceptance are separate.

Reproduce focused witnesses with `PYTHONPATH=src python3 -m pytest -q tests/test_mirror_generations.py` and `node --test tests/test_mirror_reader_reentry.js`. For deliberate break in an isolated checkout, save the candidate source bytes, substitute `git show e626002:src/mirror_reader.py`, run the named test from proof.json expecting five assertion failures, restore the saved bytes exactly, verify SHA256, and rerun expecting zero failures. Never mutate an active installed mirror for this proof.
