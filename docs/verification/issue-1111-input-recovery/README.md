# Issue 1111: input recovery and owner exclusion normalization

The first task remains incomplete. Fresh authenticated reads of issue #428 and
all 22 comments match the retained source snapshot exactly. The original audit
revision `c88282b` resolves remotely and locally to
`c88282b063d6b79efbac2103e5769166ea7b27f2`, tree
`978e6465677857e41f1de247e7a87e4b96799d35`. This recovers the revision identity,
not the audit bytes or original cohort. See [recovery.json](recovery.json).

All 71 requested identities remain unidentified. A repository-scoped search for
the original audit name returned the existing gap ledger, not the audit. The
fresh [runner inventory](runner-inventory.json) exits 2 because the selected
default Brain cannot be opened. Its counts remain null. Neither later
populations of 95/122 nor the saved 37-case report supplies the missing original
membership mapping. Only `stranske/Orchestrator` was read remotely; the excluded
repository was not inspected.

The source change advances the owner boundary in the existing inventory
exporter. Whitespace around the repository component of a target previously
bypassed exclusion and could evaluate excluded timestamp and outcome evidence.
The exporter now trims that component and registers the same predicate with
SQLite, so SQL selection and manifest reconciliation apply one rule. Original
target strings remain in identity-only exclusions. Permitted name lookalikes
remain eligible; count discrepancies and unverified-cohort flags remain visible.

The expanded private SQLite fixtures raise on excluded timestamp or verdict
reads. Against parent source, four inventory cases and three gap-export cases
fail. The candidate passes 32 inventory tests, 6 gap-export tests, 8 existing
reader/Gate CLI checks and the six-file related suite. These checks cover spaces,
tabs, line breaks, Unicode whitespace, mixed case, absent and contradictory
inventory, both manifest versions, unchanged database/manifest bytes and no
SQLite sidecars. [validation.json](validation.json) binds the logs and source
hashes. They prove the reader boundary with synthetic fixtures, not operational
cohort acceptance.

The checkout's `.git` directory is read-only: staging fails when creating
`.git/index.lock`. Source and tests were committed using separate Git metadata
at `/tmp/issue-1111-delivery/.git`, commit
`1cffa38fb703552ce18ccff73f7a24ae714594a9`. The complete transferable
[source commit patch](source-commit.patch) is retained here, so delivery does not
depend on a temporary directory. The checkout branch itself was not advanced.

No task or acceptance checkbox is marked complete. Successor work remains with
[issue #1111](https://github.com/stranske/Orchestrator/issues/1111), starting with
the authenticated original identity manifest/audit location and an accessible
owner-permitted Brain snapshot. Then use the retained inventory and per-case gap
exporters; do not substitute a later cohort. The named pytest experiments are
also blocked in this runner: none of its five Python interpreters has pytest,
and the attempted PyPI installation failed DNS resolution. No requested mutation
or named acceptance pass is claimed. Real closer observations, persisted
role/cost reconciliation, individual dispositions of the 16 retained CLI
failures, and completion metadata publication remain pending.

The blocker comment on PR #1112 and `needs-human` label on issue #1111 were
attempted as instructed. Both tools rejected publication because approval is
required and this session's policy is `never`; no remote mutation completed.
[publication.json](publication.json) retains those errors and the fresh PR
readback showing #1112 open with `isDraft=false`.
