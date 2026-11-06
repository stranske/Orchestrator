# Issue 1104: retained metadata generator verification

- [x] Retain the merged generator and prior productive tooling. Retrieve the complete metadata test file and verify its narrowly claimed behavior.

Retrieved the complete 135-line `tests/test_adjudicator_completion.js` through
the authenticated GitHub file connector at merged PR #1092 revision
`36074bc9882593a2b1948e4618a305b9ea0ddee2`. Its returned Git blob ID and the
locally computed blob ID both equal `72630620a3381fb18ca4439d632bf2d2cdd061ea`,
also matching this checkout's original test file. All seven original tests
passed before edits on parent `ec6e251c360bb72b248f0fa999b03091032b951e`.

Source: [complete merged test file](https://github.com/stranske/Orchestrator/blob/36074bc9882593a2b1948e4618a305b9ea0ddee2/tests/test_adjudicator_completion.js).

The new regression exposed an input-validation defect: an orphan status end
marker before a valid start/end block was accepted. The generator now validates
the first end marker, rejecting reversed or duplicate markers in either target.
The regression failed with `Missing expected exception` before the source fix
and passes afterward. Additional tests cover replacement of one existing block,
Unicode body hashing, open/closed issue lifecycle preservation, idempotence, and
CLI rejection with empty stdout and unchanged input files.

The generator remains a read-only proposal generator for #1074/#1075. Original
tests still verify checklist correction, retention of delivered tooling,
original-body SHA256 preconditions, lifecycle preservation, and explicit
unverified-publication and incomplete-acceptance flags. The fixture, inventory
exporter, and receipt verifier remain byte-identical to merged revision #1092.

Verification with Node v24.22.0 on the recorded parent plus this change:

| Command | Result |
| --- | --- |
| `node tests/test_adjudicator_completion.js` | 10 passed |
| `node tests/test_adjudicator_receipts.js` | 9 passed |
| `node tests/test_adjudicator_inventory.js` | 26 passed |
| `node --test-name-pattern='retro saves Gate gaps' tests/test_adjudicator_retro_cli.js` | 4 passed |
| `node tests/test_adjudicator_retrospective_batch.js` | 1 passed |
| `node tests/test_adjudicator_closer_shadow.js` | 9 passed |
| `node --check scripts/reconcile_adjudicator_completion.js` | Passed |
| `node --check tests/test_adjudicator_completion.js` | Passed |
| `git diff --check` | Passed |

Only the first task is complete. These checks preserve historical and synthetic
tooling; they do not authenticate the original cohort, run the three named
acceptance tests and specified mutations on current source, supply a real closer
observation, reconcile operational costs, disposition the 16 CLI failures, or
publish metadata corrections. Those tasks and all operational acceptance
criteria remain pending. The full CLI suite was not run in this focused change.

The checkout's `.git/index.lock` cannot be created because `.git` is mounted
read-only. The source/test commit is therefore prepared with separate Git
metadata under `/tmp/issue-1104-generator-delivery`; `source.patch` there carries
the commit for transfer. Workspace changes remain available to automation.
