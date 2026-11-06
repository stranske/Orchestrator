# Issue 1111: individual CLI failure reconciliation

Task 6 advances but remains incomplete. The fresh baseline reproduces all 16
names in the retained retrospective-batch receipt: 51 passed, 16 failed. The
delivered test change produces 65 passed, 2 failed, with the same 67 tests and
zero skips. [dispositions.json](dispositions.json) accounts individually for
every retained failure, with original, fresh-baseline and candidate log lines,
source references, explanations and explicit remaining work.

Four dispatch cases expected the obsolete 180-second timeout. They now require
the shared 600-second budget and verify that each persisted case records the
actual transport budget. Cost cases now distinguish positive whole-run receipts
from incomplete zero-dollar store telemetry. The zero-dollar case remains
unknown; its disappearance cannot preserve a measured-cost denominator. Existing
summary-only zero-cost rendering remains covered separately. Exact summaries
and rendered lines also check retry/advice fields, with UNKNOWN for old reports
that lack those fields.

The weekly-review fixture now supplies the unrelated feature-goal reader and
isolates post-merge runtime reporting. Diagnostics identified their history
write and Brain access before changing the fixture. The actual retrospective
reader and renderer remain active, as do byte-equality, forbidden-call,
informational-only and malformed-report checks. None of the original tests is
removed or disabled. Production Python files remain byte-identical to the
recorded parent.

Both remaining failures are the numeric run/attempt ordering cases. The actual
verifier selector chooses the correct NON_PASS decision, but retrospective
finding lookup compares complete enriched dictionaries from the full comment
set and a singleton comment. Their `_lifecycle_history` arrays differ, so lookup
returns `merge-bound verifier finding comment unavailable` after one request,
before pagination or dispatch. [diagnostic.log.gz](diagnostic.log.gz) retains
both case records. Its [diagnostic.patch](diagnostic.patch) recreates the
diagnostic test copy from the recorded parent; it only adds reporting and the
six initial assertion repairs, and runs against unchanged production code.
The original fixture diagnostics also identified the unrelated
`feature-goals-history.jsonl` write. No requested safety mutation is claimed.

The next source repair must match the selected finding through its trusted
origin/semantic identity while preserving author, target, revision and verdict
checks. Rerun both numeric cases, the complete CLI suite and rejected-marker
checks afterward. This runner has neither pytest nor Black; the package
installation attempts found no available distributions. A Python repair cannot
be committed here under the required Black gate. The requested three named
pytest tests were attempted with `-m "not slow"` and executed zero tests:
[named-attempt.log](named-attempt.log). No named pass, source mutation or
restoration evidence is asserted.

Other verification passes: all six related JavaScript test files, 23 module
self-checks and 11 existing evidence unittests. The complete CLI command exits
1 because the two source failures remain. [validation.json](validation.json)
binds commands, source hashes and raw logs. These are private-fixture checks,
not historical cohort, real closer, role/cost or operational acceptance proof.

Recent commits `cffc0da`, `ee0ffdd` and bootstrap `43e8867` were reviewed before
continuing. They do not satisfy any full task. All 26 task/acceptance checkboxes
remain unchecked. Original identities and a permitted Brain remain unavailable;
the owner-excluded repository was not accessed. Source and successor ownership
remain [issue #1111](https://github.com/stranske/Orchestrator/issues/1111) and
[PR #1112](https://github.com/stranske/Orchestrator/pull/1112).

Fresh authenticated readbacks show issue #1111 open and PR #1112 open with
`isDraft=false`, but the PR still has its premature auto-closing preamble. Body
correction, thread reply and `needs-human` publication were rejected because
connector writes require approval under policy `never`. No remote mutation or
review-thread resolution completed. [publication.json](publication.json) records
the errors/readbacks; the proposed body and thread reply are retained alongside
it for the authorized publisher. This does not complete metadata reconciliation
for the older issues/PRs.

The checkout's `.git` is read-only: staging fails at `.git/index.lock`. The
meaningful test commit was made with separate metadata at
`/tmp/issue-1111-cli-delivery.git`, commit
`d4e00a9b47655c2391854c32283ae9334e4f09c5`. Its transferable
[source commit patch](source-commit.patch) is retained here; the checkout branch
was not advanced. No `.py` files change, so the Python formatting gate does not
apply to this delivered JavaScript test change.
