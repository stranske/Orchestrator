# Issue 1091: successor completion metadata

The previous two commits were reviewed before continuing. Their metadata
proposal and six CLI test repairs do not complete a task: publication failed,
the original identity manifest is unavailable, and ten CLI cases remain open.
All operational task and acceptance checkboxes remain unchecked.

Authenticated reads in this round confirmed #1074 and #1091 closed, with
#1075 and #1092 merged. In particular, #1092 is not an open delivery PR.
This supersedes the earlier completion note's open-PR observation. No later
open successor was authenticated. This run did not create or change a PR.

`scripts/reconcile_adjudicator_completion.js` now accepts an optional
`successor` snapshot for #1091/#1092 in addition to #1074/#1075. It produces
body proposals with original-body SHA256 and captured lifecycle preconditions.
The successor proposals clear the unsupported tasks and acceptance claims in
both checklist copies, preserve unrelated delivered-work checkboxes, replace
the standalone `Closes #1091` directive, and explicitly distinguish closed or
merged lifecycle from completed operational acceptance. No state mutation is
proposed. The tool never publishes its output or claims remote verification.
It rejects missing lifecycle evidence and changed checklists before emitting
any proposal, and repeated generation is idempotent.

The new successor fixture contains the captured bodies and lifecycle fields;
it is a regression input, not authorization to overwrite newer remote bodies.
To reproduce the four body proposals, combine the existing and successor
fixtures into a snapshot with a `successor` member and pass that file to the
CLI. Fetch fresh complete bodies and compare preconditions before publication;
re-read all affected records afterward before completing the metadata task.

Verification:

```sh
node tests/test_adjudicator_completion.js
node tests/test_adjudicator_inventory.js
node tests/test_adjudicator_receipts.js
node --check scripts/reconcile_adjudicator_completion.js
git diff --check
```

All 13 metadata, 26 inventory and nine historical receipt tests pass. These
are not current-source named pytest acceptance or real operational evidence.
No Python source changes. Black and pytest remain absent, and a temporary
package installation failed because PyPI could not resolve. The required
Python formatting gate therefore blocks committing Python repairs to the
demonstrated cost and finding-selection defects. A fresh complete CLI run
(`node tests/test_adjudicator_retro_cli.js`) confirms the prior diagnosis:
57 of 67 tests pass, with exactly the same ten retained failures. The raw
transcript is retained at `/tmp/1091-cli-current.log`; the prior committed
diagnosis identifies each failure. No cost assertions were weakened.

Automatic approval review rejected the concrete #1075 body correction, #1091
blocker comment and `needs-human` label because connector writes require
approval while the run policy is `never`. Remote
metadata and checkboxes remain unchanged. The checkout's `.git` mount also
rejects staging as read-only. The source/test changes and a transfer commit
are retained using separate Git metadata under `/tmp/1091-successor-delivery`;
workspace files are left for automation to collect.
