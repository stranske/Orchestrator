# Empty Gate evidence recovery

Empty initial or paginated commit lists must become a `ValueError` evidence gap,
preserving the batch caller's existing recovery path. Raw original-source results
(two failing tests), candidate focused results (40 passing tests), and the 2,607-test
collection are retained losslessly. Broader issue #428 acceptance and the original
#511 provider CONCERNS remain open.

The full private verifier executed 2,607 passing pytest tests, 103 of 103 selftests,
and all five capability gates. It exited 1 solely because the starting floor of
2,605 lagged the new collection of 2,607. `--update-floor` corrected the floor to
2,607 while preserving every ceiling. The original exit 1 and raw log are retained;
no repeat exit 0 or live activation is claimed. Fresh hosted CI remains required.

The later context-shape repair and its current-head regression proof are recorded
separately in `context-shapes/`. The historical results above describe their original
revision and are not a full-suite claim for that later change.
