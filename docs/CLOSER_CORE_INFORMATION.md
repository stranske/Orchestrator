# Closer core request capture

`GitHub.snapshot` owns acquisition; `closer_evidence._source_section` retains the
request bytes; the existing disposition and action guards own acceptance.
These are separate contracts. A complete capture does not prove implementation
or authorize merge, verification dispatch, or issue closure.

## Captured request identity

- Assigned issues and GitHub closing-issue references retain the issue body.
  The existing authored-preamble/branch lineage reader remains the fallback
  when the closing-issue connection is empty. Generated status inventories are
  excluded from that fallback.
- When the complete closing-issue inventory and existing fallback find no issue,
  retain the PR body as `kind: pr_request`, with `via: github_pr_body`.
  `source_inventory.status: no_linked_issue` records that observed linkage result;
  it is not a claim that arbitrary prose has no unrecognized references.
- PR body capture also covers an explicit `workflow-source:local_request`
  request, such as Portable-Alpha-Extension-Model PR 2390. Its cross-repository
  `Refs` are context, not invented source-issue requirements. The captured body
  is the current GitHub representation, not an independently recovered original
  chat transcript or a claim about an earlier body revision.
- Every newly captured source records the available title, GitHub URL, acquisition path, body SHA256,
  available GitHub update timestamp, and request revision. Packet source rows
  retain that provenance alongside the exact body file. The existing snapshot
  revision and packet source digest bind the complete inventory. Acceptance source
  digests also bind the captured request revision, so title/revision changes cannot
  reuse prior acceptance. Titles use the same redaction/truncation boundary as bodies. Old retained
  packets/history are not rewritten.

## Failure boundaries

An absent body key or nontext body is `CoreInformationError` with category
`body_transport_omission`; an explicit GitHub null body is empty authored content.
A returned PR with the wrong number is `identity_mismatch`; a null GraphQL PR is
`object_unavailable`. None becomes an empty linked-issue inventory. Existing
fetch exceptions and round capacity/deadline controls still propagate; the
pipeline records the failed per-item read and a later invocation can reacquire
it. There is no hidden retry loop or provider request in this change.

A GitHub read failure does not establish deletion: an inaccessible object can be
indistinguishable from an absent one. The API adapter currently returns status-
free parsed bodies from `gh_capacity.gh_run`; do not infer actual absence from a
transport exception or a generic Not Found response. An explicit lineage that
resolves to a PR instead of an issue still fails the existing identity guard.
Incomplete linkage/review pagination and ambiguous delivery PR selection keep
their existing failure/hold behavior. A complete empty delivery-PR inventory is
an issue with no observed delivery, not a missing source issue.

## Consumer and release boundaries

A `pr_request` participates in evidence and request coverage, but never in issue
closure. A merged PR with no linked issue still needs retained acceptance proof
and merge-bound verifier PASS to complete. Unknown requirement grammar remains
non-authorizing: the Tasks/AC parser is unchanged, and free-form original prose
is retained without manufacturing criteria. This fixes capture, not general
free-form acceptance interpretation.

Diagnostic artifacts, raw NON_PASS downloads, and review pagination callbacks
are separate enrichment/transport boundaries. Their omission does not mean the
PR identity or original request is absent. PR 857 owns reasoning admission and
shared collector bindings. This change leaves those functions and guards alone;
its snapshot/source-row additions compose with that PR. Normal merge/release
verification must cover the combined tree before runtime installation. No queue,
campaign, historical UNKNOWN, runtime, or baseline packet was modified or replayed.
