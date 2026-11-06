# Issue 1074: retained acceptance receipts and merge verifier investigation

- [x] Retrieve and validate the required named-test mutation receipts, or reproduce them in isolation; inspect the failed merge-commit verify.py log and resolve or substantiate its relevance.
- [ ] Run all three named acceptance tests on the current source.
- [ ] Establish the real 71-dispute inventory, execute its retrospective and observe a real contested closer disposition.

The first incomplete task remains blocked: this runner has neither the original
Brain nor the requested cohort manifest. The preceding inventory, batch and
closer receipts document that limitation. This round therefore advances the
next unblocked task, as suggested by the task prompt.

`scripts/verify_adjudicator_receipts.js` checks the retained named-test packet
against immutable Git bytes. It binds the manifest to merged PR #511, retrieves
the README at its declared revision (its current bytes differ as documented),
checks the validation and compressed bundle bytes, and verifies every member
digest. It recomputes each production mutation from the evaluated source and
checks exact restoration, the named pytest commands, native JUnit case identities,
suite counts, assertion locations and console results. It executes no receipt
commands, pytest, replay or role dispatch. Python supplies only its standard XML
parser. Only temporary IO files are created; no Brain or report is opened.

The historical evaluated source is `6ed980924ec1d7d83a06f9b6a181dc76ba5eb910`.
Its focused suite reports 39 passes. The outcomes mutation has one assertion
failure and one restored pass; the unconditional-offer mutation has three
assertion failures among five cases and five restored passes. Four of the five
bound source/test files have since changed. These results validate historical
receipts; they cannot complete current-source named acceptance. Pytest remains
unavailable in this runner, and no Python files change in this round.

The failed [merge CI run](https://github.com/stranske/Orchestrator/actions/runs/37615810945)
evaluated `b9267a08939a53d6bbbff7d098dad5c5e0f04dc3`. Its
[verify.py job](https://github.com/stranske/Orchestrator/actions/runs/37615810945/job/112773736256)
was retrieved through the GitHub connector and inspected. It reports 2,579
passed, zero failed, 26 skipped, and 2,605 collected. Its sole PROBLEMS entry is
the stale collection floor of 2,592. The concurrent
[reconciliation job](https://github.com/stranske/Orchestrator/actions/runs/37615810945/job/112773736215)
raised both floor counts to 2,605, preserving all ceilings, and committed
`9c84b8fc066a4d34f7d0fb318133c012deb38f9d`. Git confirms that commit directly
follows the evaluated merge and changes only `.verify-floor.json`. Exact
relevant log lines are retained in `merge-verify-excerpt.txt`; normalized
provenance is in `merge-verify.json`. This substantiates the historical failure's
cause and already-landed repair; it does not claim a fresh full verifier pass.

Validation performed on parent `0afad3f1a3a4e498800277f71717f5a73deb5b7a`:

```sh
node scripts/verify_adjudicator_receipts.js
node tests/test_adjudicator_receipts.js
node tests/test_adjudicator_inventory.js
node tests/test_adjudicator_retrospective_batch.js
node tests/test_adjudicator_closer_shadow.js
node --test-name-pattern='dispute inventory|retro saves Gate gaps' tests/test_adjudicator_retro_cli.js
python3 src/adjudicator_retro.py --selftest
PYTHONPATH=src python3 -m unittest discover -s tests -p test_adjudicator_retro_evidence.py
```

All pass: nine new receipt tests, seven inventory tests, one synthetic batch
test, nine synthetic closer tests, eight existing inventory/gate tests, 23
module selftest checks and 11 evidence unittests. The new tests reject stale
source hashes, incorrect mutation/restoration hashes, another named command,
skipped or substituted JUnit cases, unrelated assertion failures, inconsistent
case outcomes, missing receipts and changed retained bundles. The final test
checks the floor explanation against the historical Git JSON and retained log
lines. JavaScript syntax and diff whitespace checks pass. The full CLI suite's
16 previously recorded failures have not been repaired or rerun by this change.

The workspace Git metadata is read-only: staging fails to create
`.git/index.lock`. A source commit and transfer patch are prepared under
`/tmp/1074-receipts-delivery`; all source edits remain in the workspace for
automation to collect. Operational acceptance stays incomplete, and the PR must
remain open and ready for review without closing #1074.

The GitHub connector rejected the verified checkbox update, removal of
`Closes #1074`, blocker comment and `needs-human` label: these mutations require
approval, while this run's approval policy is `never`. The checked task above
is local evidence only; the remote checklist and closing reference remain
unchanged. PR #1075 was confirmed open and ready for review.
