# Issue 1104: original-source recovery and identity accounting

- [ ] Recover the authenticated requested cohort from original evidence and reconcile it with the permitted inventory.

The cited October 4 audit and companion owner decisions were located through
the authenticated Dropbox connector. Their complete extracted text is saved in
`audit.extracted.md` and `decisions.extracted.md`. `recovery.json` records file
identities, Dropbox revisions, modification times and extracted-text SHA256s.
These are extracted text, not downloaded original bytes. Terminal blank lines
were removed; the receipt retains lengths and hashes for both connector text
and saved files. The current program's
U-ADJ row is retained as an explicitly partial excerpt, with its later
modification time. It is not an original membership record.

The companion decisions explicitly request a shadow test on the 71 disputes.
Neither recovered document enumerates those dispute identities. The original
issue's short source revision was resolved through the authenticated GitHub
connector to `c88282b063d6b79efbac2103e5769166ea7b27f2`. These findings supersede
only the audit-location and unresolved-revision gaps in the
[previous recovery ledger](../issue-1104-cohort-recovery/reconciliation.json).
All 71 original identities remain unidentified. Later aggregate counts and
individual retrospective references cannot establish original membership.

The inventory exporter now includes `requested_identity_accounting`: the
expected and supplied counts, unsupplied slots, excess supplied cases and
counts by reconciliation status. Unsupplied slots are separate from supplied
identities whose run is missing, unavailable, excluded or contradictory.
Without a manifest, status counts stay null. The gap exporter shares these
counts and retains excess supplied identities. All accounting is scoped to
the supplied manifest; none authenticates original membership.

The fresh `runner-inventory.json` still reports an unavailable Brain, null
observed counts and 71 unsupplied slots. Its unavailable status is exit 2,
not a zero-case success. No original manifest or complete owner-permitted
inventory was recovered. Only Orchestrator documents and metadata were opened;
no owner-excluded repository was inspected. Original exclusion identities and
their count remain unknown. No operational checkbox is newly completed.

Validation: 32 inventory tests and seven case-gap tests pass, including five
new regressions for mixed statuses, an equal-sized later inventory without a
manifest, both manifest versions against an unavailable Brain and excess
supplied identities. The seven related Node test files pass together. Syntax
and diff whitespace checks pass. These are reader/accounting regressions, not
the three named pytest acceptance tests or a real closer observation. No
Python file changed, so the Python formatting gate does not apply.

The checkout's Git metadata is read-only (`.git/index.lock` cannot be created).
The source/test change is committed using separate Git metadata under
`/tmp/issue-1104-identity-accounting.git`; the transfer patch is
`/tmp/issue-1104-identity-accounting.patch`. Workspace edits remain available
for automation to collect. No remote PR was created or changed; fresh readback
found the historical PR #1105 already merged and non-draft.
