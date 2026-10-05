# PR #443 review recovery

Reviewed starting head: `b387371c247fb991e3546fe681dba57ce64ffe15`.
The eight CodeRabbit threads were active and unresolved when inspected. Their
previously checked recovery tasks were premature: the source and test fixture
still contained all eight reported defects.

## Repairs and focused proof

| Review comment | Repair | Regression proof in `tests/test_role_prompt_batch.py` |
| --- | --- | --- |
| 4179391581 | Describe manifest-only batch output without dispatch or replay proposals. | `test_batch_cli_without_dispatch_emits_manifest_only` |
| 4179391587 | Check confidence type before set membership; retain later item processing. | `test_issue_body_confidence_errors_do_not_abort_later_batch_items` |
| 4179391591 | Recognize LF and CRLF section headings. | `test_issue_body_accepts_crlf_sections` |
| 4179391593 | Validate optional field types for every batch item before routing or execution. | `test_batch_optional_fields_are_validated_before_any_item_runs` |
| 4179391598 | Reject dispatched replays without a backend; preserve undispatched and explicit-backend replays. | `test_replay_requires_backend_only_when_dispatched` |
| 4179391599 | Read batch input and write bodies and manifest explicitly as UTF-8. | `test_batch_cli_uses_utf8_for_input_bodies_and_manifest` |
| 4179391602 | Return failure and put diagnostics on stderr; withhold plain body output after backend, validator, or recording failure. Also withhold batch bodies after recording failure. | `test_single_issue_cli_failure_withholds_body` (three cases), `test_batch_recording_failure_withholds_body` |
| 4179391609 | Stub `feedback._capability_daily_heartbeat` as well as the role capability event in `private_brain`. | Both named acceptance tests and all dispatched batch tests use this fixture. |

## Validation

Using the repository's pinned pytest 9.1.1 and Black 26.5.1 on Python 3.12.3:

```text
pytest tests/test_role_prompt_batch.py tests/test_roles_lineage.py -q -m "not slow"
30 passed in 1.13s

pytest tests/ --collect-only -q -m "not slow"
1608 tests collected in 2.29s

python3 src/roles.py --selftest
roles.py selftest: OK

black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
All done!
305 files would be left unchanged.
```

The collection and pass floors increase from 1598 to 1608 for ten new regression
nodes; no skip ceiling changes. The focused run verifies those ten additions;
collection verifies the suite count, not the full suite verdict.

Loading the original head's `src/roles.py` while retaining the new regression
tests produced `9 failed, 12 deselected in 0.36s`. The failing nodes cover
confidence, CRLF, optional field preflight, replay provenance, UTF-8, three
single-item CLI failure cases, and batch recording failure.

Temporary pytest plugins applied each requested deliberate break without
changing repository files: replacing `_validate_issue_body` with the dispatch
validator made the first named acceptance test fail; removing the batch's
explicit backend before each item ran made the second named acceptance test
fail. Removing both plugins restored `2 passed in 0.24s`.

Tool sources came from the official tagged repositories through the read-only
GitHub connector because local pip installation could not obtain packages.
Tools and plugins live under `/tmp`, outside the repository. Black's temporary
launcher uses a single thread worker instead of the sandbox's hanging process
pool; it uses the same Black formatter and file selection.

## Remaining external state

The GitHub connector rejected PR-body reconciliation, the blocker comment, and
the `needs-human` label because approval is required and this run has approval
policy `never`. Git staging failed with `Unable to create .git/index.lock:
Read-only file system`; these repairs remain uncommitted in the working tree.
PR #443 was re-read and remains open, ready for review, at the starting head.
No reviewer thread was self-resolved. Reviewer disposition and a published-head proof remain required
before checking the collective review task. The proposed reconciliation is to
check the named acceptance after publication and verification, and retain the
review task until the originating reviewer supplies disposition.
