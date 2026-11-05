# Retained retry identity results

Source commit `f0acc6900ba1f860d02868f61da987a4afa0a592`, parent
`ba918d748be8dc58d50741734a2930a10a20ad69`, fixes identity attribution in the
repository-specific read-only inventory. Explicit nested case/run/target/verdict
fields must match the root case at every depth. Missing repeated fields remain
supported. Contradictory or unverifiable identity fields cause refusal before
manifest or native receipt reconciliation, with no partial CLI JSON and no
conflicting values in errors. Original report, journal and manifest bytes remain
unchanged. This validates attribution, not authenticity or retry eligibility.

All [122 focused checks](validation.json) pass: 67 inventory, 33 acceptance runner,
22 ledger. Three added controls failed before implementation. Controls cover
each identity field at two nesting depths, compact and full histories, missing
root identity, malformed explicit identities, native locator/manifest borrowing,
CLI refusal and input preservation. JavaScript syntax and whitespace checks
pass. No Python files changed.

The [pre-execution plan](plan.md) precedes the [required-test receipt](acceptance-run/checks.json).
Both required pytest commands and independent collection failed before collection
because the selected Python 3.14.10 interpreter has no pytest. This checkout is
not an identified accepted deployment; no acceptance test pass is claimed.

GitHub #579/#1072/#1096 and Dropbox searches recovered references and historical
comments, but no original retry manifest, row-level retry results, native journal
or fixed next-20 manifest. Historical comments cannot authenticate current
outcomes. Accepted/deployed/mirror identities, independent current later truth,
live writer ownership and raw October 18 proof remain unavailable. No task
checkbox is newly complete; no paid retry, canonical publication, mirror sync
or production write occurred. PR #1097 was checked open with `draft=false`.

Normal staging failed because `.git/index.lock` cannot be created on the
read-only checkout metadata. The source commit is preserved in
`/tmp/issue1096-retry-identity-git-lf3aCH` with an
[applyable patch](../../../../artifacts/issue-1096-retry-identity.patch).
The checkout branch is unchanged. Automatic approval review rejected both the
blocker comment and `needs-human` label: connector mutations require approval
while this run's approval policy is `never`. Neither remote change was applied.
