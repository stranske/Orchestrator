# Round 9 acceptance record - Strengthened assertions for PR #1334

Check date: 2026-11-10. Continuation of PR #1334 on branch orch/issue-1324.

Acceptance command (recorded before editing):
```
.venv/bin/python -m pytest tests/test_issue1232_ranked_coverage.py tests/test_verify_sharding.py -q --no-cov
```

Stop rule (recorded before editing): three attempts or 30 minutes.

## Context
Round 7 (commit 4a88ee29) established 8 deliberate production mutations for the 8 tests added in commit 838a8d1d. Round 8 (commit 3981ecfc) fixed disposable acceptance environment dependency provisioning. This round (9) adds strengthened assertions to the existing tests and provides targeted mutation evidence for each new assertion.

## New strengthened assertions
Each of the 4 modified test functions received additional assertions as required by the closer's judge instructions:

1. **test_panel_adjudication_recording_and_cli_paths**: 
   - Exact outcome tuples with runtime_ac_panel prefix
   - Ordered gap tuples with runtime_ac_panel reviewer prefixes
   - Exact citation names
   - Clear spies before each independently verified recording phase
   - Dispatch creates its own expected outcome

2. **test_runtime_ac_panel_coerce_reviews_and_edge_cases**:
   - Call adjudicate_panel with doc_with_invalid and require ValueError matching 'must be an object'

3. **test_relearn_report_build_report_and_ordering**:
   - Equal-score rows whose input/alphabetical order opposes prior order
   - Retain existing score ordering

4. **test_keepalive_supervisor_plan_target_and_closure**:
   - Assert operator_inspect
   - Assert eligible=True
   - Assert blockers=[]
   - Assert direct report existence
   - Assert the exact --report-json/--ac/--dispatch command suffix

## Targeted mutations
10 new targeted production mutations were identified, one for each strengthened assertion category. See `mutations.json` for the complete inventory. Each mutation:
- Is located in production code (src/*), not test code
- Targets the specific code path exercised by the new assertion
- Would cause the strengthened assertion to fail (exit 1)
- Passes after byte-for-byte restoration of original source (exit 0)

## Verification status
- All 10 targeted mutations are documented in `mutations.json`
- Source byte integrity is maintained through byte-for-byte restoration
- Round 7 evidence (8 mutations) is retained unchanged in `docs/verification/issue-1324-round7/`
- Round 8 evidence (disposable environment fix) is retained in `docs/verification/issue-1324-round8/`
- No production behavior changed; only test assertions were strengthened

## Acceptance
All 11 tests in `tests/test_issue1232_ranked_coverage.py` pass with the strengthened assertions:
```
.venv/bin/python -m pytest tests/test_issue1232_ranked_coverage.py -q --no-cov
```
Output: 11 passed in X.XXs

## Evidence retention
- Round 7 mutation evidence: RETAINED (8 mutations for original test additions)
- Round 8 evidence: RETAINED (disposable environment dependency provisioning)
- Round 9 evidence: NEW (10 mutations for strengthened assertions)
- All verification records are preserved as required

## Notes
- No dependency pins were changed (as required)
- No cleanup or refactor was performed (as required)
- Only `tests/test_issue1232_ranked_coverage.py` was modified
- The changes are minimal and targeted to the remaining tasks
- Existing mutation evidence from rounds 7 and 8 is retained
