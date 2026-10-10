# PR 516 transport and review recovery

The previous dispatcher accepted Gemini NDJSON without a final string response. The three new production regression cases fail on the prior source and pass after the fail-closed repair. Dispatcher tests now cover exactly 65,536 and 65,537 UTF-8 bytes with multibyte text.

The two hosted cleanup failures reproduce when a cold model catalog reaches the mocked subprocess before the private prompt file exists. Transport fixtures now disable unrelated catalog subprocess probing. Private-file mode and removal assertions remain intact.

Retrospective recovery tests now fail inside `feedback.record_completion_event`, after the run write. They require event counts `[0, 1, 1]` before repair, after repair and after stable retry. The old injection fails both partial-write cases with `[1, 1, 1]`.

`proof.json` binds raw results and exact source hashes. Focused Python: 29 PASS plus 23 subtests; CLI: 54 PASS; dispatcher selftest, lint and mypy PASS. Whole-tree collection is 2,672. Source #428's original native cost, historical acceptance and later-truth gaps remain open. No provider experiment, mirror installation or live publication was performed.

Full private-state verifier completed: **2,672 actual pytest PASS, 103/103 selftests, all five gates GREEN, 121 modules type-checked**. This proves the local checkout; fresh hosted normal/flat checks and merge guards remain required.
