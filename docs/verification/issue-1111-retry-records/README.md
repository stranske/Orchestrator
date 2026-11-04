# Issue 1111: retained retry role and cost references

Task 2 advances; no complete task or acceptance criterion is satisfied. Recent
commits `c9d15c9`, `13a0cd0`, `1021c90`, `cffc0da` and `ee0ffdd` were reviewed
before continuing. Their receipts establish partial tooling work, so all 26
checkboxes remain unchecked. The original 71 identities remain unavailable.
The fresh [inventory](runner-inventory.json) cannot open the selected default
Brain (exit 2, counts null). No owner-excluded repository was accessed remotely.

The existing case-gap exporter now accounts for nested `retry_attempts` when a
saved report is supplied. Each attempt retains its array path, complete dispute
identity, report and row digests, claimed revision and role/backend IDs. Only an
exact identity match can supply persisted lookup hints. With `--persisted-roles`,
the existing read-only transaction also checks those retry roles and costs.
Positive whole-run `ccusage` receipts remain references; zero, missing or
unavailable telemetry remains UNKNOWN. Retries do not increase requested case
counts. This exporter does not compute a combined case cost or authenticate the
original cohort or execution revision.

Missing or foreign identities cannot borrow their parent's role provenance.
Malformed histories retain specific array paths. An excluded retry withholds
execution claims for its containing case, including the parent digest that
would serialize the excluded history; descendants of that retry are not
inspected. An excluded stored backend prevents its cost read. Existing behavior
for reports without retry history is preserved. Every case and retry still has
UNKNOWN verified revision, result, provenance and cost, with operational
acceptance false.

Verification is bound in [validation.json](validation.json):

| Check | Result |
| --- | --- |
| Unmodified baseline: exporter, inventory, summary suites | 3 files passed |
| Expanded exporter tests against parent source in a temporary tree | 18 passed, 7 failed |
| Final expanded exporter suite | 25 passed, 0 failed, 0 skipped |
| Related exporter, inventory, summary, batch, closer, receipt, completion suites | 7 files passed |
| Full retrospective CLI suite | 65 passed, 2 failed, 0 skipped |
| JavaScript syntax and diff whitespace | Passed |

The two CLI failures remain the previously diagnosed numeric run/attempt order
finding-selection failures. This change does not repair Python source or replace
their [individual dispositions](../issue-1111-cli-failures/dispositions.json).
The production role-recorder test also supplies a retained retry, matching its
receipt to the production cost grader. Private fixtures check unchanged input
bytes, absent SQLite sidecars, unknown costs, identity mismatches, malformed
histories and owner boundaries. These are regression witnesses, not real cohort
or closer observations. The temporary parent comparison is not either specified
safety mutation.

The selected interpreter still lacks pytest; the named acceptance attempt runs
zero tests. Black is also absent. No Python files change, so the Python
pre-commit formatting gate does not apply to this JavaScript delivery. Neither
specified source mutation was performed, and no restoration receipt is claimed.

The source/test commit, transfer patch and hashes are retained in
[delivery.json](delivery.json). Repository staging failed at `.git/index.lock`
on a read-only mount, so separate metadata under
`/tmp/issue-1111-retry-delivery.git` holds the commit. The transferable
[source patch](source-commit.patch.gz) applies to the recorded parent. The
checkout branch was not advanced.

The current PR body correction changes only `Closes #1111` to `References #1111`,
preserving all 26 unchecked items. Automatic approval review rejected the body
update and `needs-human` label because connector writes require approval while
this session's policy is `never`. Publication receipts and a fresh readback are
retained in [publication.json](publication.json). The initial readback showed
PR #1112 open and ready for review. The final readback shows it was merged
externally at head `c9d15c9`, merge commit
`a2de7d0a7847bab9b5c153386944969e64a89b05`. This run performed no PR mutation or
merge. The merged head does not include this round's source/test commit. No
remote metadata reconciliation is claimed.

The originating handoff is [issue #1111](https://github.com/stranske/Orchestrator/issues/1111)
and merged [PR #1112](https://github.com/stranske/Orchestrator/pull/1112). This
round's patch needs delivery through successor work; no open successor has been
verified here. Source #1111 is also now closed with reason `completed`, despite
its unchecked work; the fresh source readback is retained in the publication
receipt. Recover the
authenticated original manifest and permitted Brain first, then export real
case and retry references. Summary reconciliation still does not consume these
new retry references; it deliberately retains its unresolved-attempt gap. The
named experiments, real contested observation, two CLI source repairs and
authorized metadata publication remain unfinished.
