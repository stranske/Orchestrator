# Pre-dispatch results, 2026-11-03 UTC

The pre-execution contract is frozen in [pre-dispatch-plan.md](pre-dispatch-plan.md).
The command ran at 18:51:58 UTC against the recovered historical extractor text.
Its 2,386,541 local bytes and SHA-256 match the earlier recovery file; original
Dropbox byte identity remains unverified. Text was restored with the same trailing
newlines as that earlier local file. The first verification wrapper was refused
by the sandbox's child-process pipe restriction before it could execute the
exporter. Using file-backed subprocess output allowed the one real-input check.

[pre-dispatch-observation.json](pre-dispatch-observation.json) records the exact
command, timestamp, input hash, counts, pre-dispatch reasons and full stdout hash.
The full original row/paid-locator inventory remains in historical-inventory.json.
The expected 58 retained rows, population 40, 38 unique paid identities and saved
graded 0 matched. The earlier snapshot has no native publication marker; this
does not contradict the later historical canonical native publication. Neither
can authorize a generic retry from this runner.

The new optional `--journal` reader observes an existing native SQLite journal in
one read-only transaction without initialization or schema repair. It separates
report/journal/shared paid identities and identifies possible started attempts
without backend ids. Receipt payloads are not emitted. State-specific recovery
steps retain UNKNOWNs and require native authentication before grading. This is
an inventory, not a replacement for the native admission/publication machinery.

No authentic native journal was supplied or found in this runner. Native attempt
counts, journal-only paid identities and report/journal union counts are therefore
null, not zero. Live writer ownership, source/deployment/mirror bindings, immutable
case qualification, authenticated receipts and current independent truth remain
unverified. No provider invocation, production dispatch or publication occurred.

[pre-dispatch-checks.json](pre-dispatch-checks.json) binds the tested source hashes
to parent ed048b3417585c8d3f5353b098341eeab7c2bd8c and preserves command/log hashes:

- 40 inventory tests pass, including active writer isolation, empty/missing/foreign
  journals, recursive paid-history reconciliation and unchanged fixture bytes.
- The existing module selftest passes 23 checks; 11 existing evidence tests pass
  under the selected Python 3.14.9 interpreter (with existing ResourceWarnings).
- The four named pytest nodes and full retrospective suite both exit 1 before
  collection: `No module named pytest`. This is not accepted-revision test evidence.
- JavaScript syntax and whitespace checks pass. No Python files changed.

No acceptance task can be completed from these results. Current native receipts,
live writer coordination, original requested retry identities and fixed next-20
manifest are still needed; the recovery checkbox correction remains local because
GitHub rejected connector writes under the run's approval policy.

Normal staging also failed: `.git/index.lock: Read-only file system`. The source,
tests and receipts are preserved in an isolated Git directory under
`/tmp/issue-1096-pre-dispatch/.git`, with the exact checkout HEAD as parent, and
in `artifacts/issue-1096-pre-dispatch.patch`. The checkout branch itself is
unchanged. Blocker-comment and `needs-human` label attempts were also rejected
because connector mutations require approval while the policy is `never`.
PR #1097 was rechecked open with `draft=false` before handoff.
