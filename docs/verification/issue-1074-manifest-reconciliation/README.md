# Issue 1074: supplied-manifest inventory reconciliation

- [ ] Establish a read-only inventory of the requested 71 disputes.

The exporter now accepts `--manifest FILE` to reconcile requested case identities
against a fixed-time Brain observation. The JSON input has `schema_version: 1`
and a `cases` array of `{ "run_id": "...", "target": "owner/repo#N" }` objects.
Duplicate run IDs, blank identities, invalid schemas and unreadable files fail
before publishing an inventory. The report binds the exact input bytes with
SHA-256 and retains the supplied case order.

Each requested identity is included, excluded with its existing selection
reasons, missing from the Brain, unavailable, or associated with a different
observed target. Every status other than included has a per-case evidence gap.
Unexpected eligible cases are listed separately. Equal counts cannot hide a
substituted identity, and a manifest with fewer than the requested number of
cases cannot produce an exact match. An unavailable Brain retains null observed
counts and unexpected cases while still accounting for all supplied identities.

`manifest_reconciliation.exact_match` describes the supplied manifest only.
`requested_cohort_verified` remains false: input provenance must independently
establish that the manifest identifies the original historical cohort. The tool
does not replay, dispatch roles, open reports, or change outcomes.

On the original Brain host, with a justified observation time and a recovered
identity manifest:

```sh
node scripts/export_adjudicator_inventory.js --db /path/to/existing/brain.db --observed-at UNIX_SECONDS --manifest /path/to/requested.json > inventory.json
```

Seven new regressions pass alongside the seven existing inventory tests. They
exercise 71 distinct identities sharing one PR, equal counts with substituted
cases, target mismatches, excluded and absent runs, an unavailable or empty
Brain, manifest size discrepancies, byte preservation and CLI validation.
Existing synthetic retrospective, closer and receipt tests pass, as do eight
focused inventory/Gate regressions, 23 retrospective selftest checks and 11
evidence unittests. See [validation.json](validation.json) and the retained
[inventory test log](inventory-tests.log). No Python files changed. The full
CLI suite and current-source named pytest acceptance were not rerun; prior
receipts' 16 baseline failures are not repaired by this change.

The [fresh runner observation](observed-inventory.json) still reports an
unavailable Brain, with null counts. The canonical retrospective report and
original 71-case manifest are also unavailable. The real inventory, retrospective,
closer invocation and report reconciliation therefore remain incomplete; this
change advances inventory tooling without completing an operational checkbox.

Workspace staging failed because `.git/index.lock` cannot be created on the
read-only mount. A separate source commit and transfer patch are prepared under
`/tmp/1074-manifest-delivery`; source edits remain in the workspace for automation.
PR #1075 was inspected and is open with `draft=false`. Its premature `Closes
#1074` reference still requires removal while operational acceptance is pending.
The connector rejected that body correction, the blocker comment and the
`needs-human` label: each requires approval while this run's policy is `never`.
Remote metadata is unchanged.
