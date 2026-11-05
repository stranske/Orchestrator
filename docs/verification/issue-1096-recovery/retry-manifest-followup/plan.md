# Retry manifest follow-up, 2026-11-03 UTC

Before execution, evaluate checkout source revision
`191dfb9f5bbc27a357cef8b5f16ad306f5eb3f45` using the selected repository Python:

```sh
node scripts/run_adjudicator_acceptance.js \
  --python /opt/hostedtoolcache/Python/3.14.9/x64/bin/python \
  --revision 191dfb9f5bbc27a357cef8b5f16ad306f5eb3f45 \
  --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/retry-manifest-followup/acceptance-run
```

Expected result: collection, four named tests and full retrospective suite fail
before collection with missing pytest. Both ordinary and isolated pip attempts
found no available pytest distribution in this run; do not retry installation.
No accepted deployment binding is established by this checkout identity.

Stop after one collection and one invocation of each required group, each bounded
to 60 seconds with `-m "not slow"` and the existing runner's temporary state.
Preserve commands and output. Do not dispatch, publish, initialize production
state, write mirrors, or mark a test acceptance checkbox complete.

With required pytest execution blocked, advance original retry-row accounting:
allow the read-only report inventory to match a supplied original manifest to
retained rows. Verify matching, missing, ambiguous and conflicting identities,
manifest order, unchanged input bytes, CLI behavior and attribution using Node
regressions. Supplied manifest matching cannot establish original provenance,
authenticate decisions, or authorize retries. The original manifest and native
receipts remain unavailable; fixture verification cannot complete that task.
