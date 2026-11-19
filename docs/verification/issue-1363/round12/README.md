# Partial archive failure regression

The remaining post-deployment audit task stays unchecked. This Linux runner
does not have the production runtime or installed mirror generation. The audit
against the latest recorded host paths exits 1 with ENOENT (`deployment-audit.log`).
No fresh production collector observation or deployment acceptance is established.
The historical host-round7 audit still records 188 refusals and unmet acceptance.

Reviewed commits `a47c0473`, `52c5d021`, `a9ea8364`, and `b9d8a9dd` before
continuing. The PR body already has the supplied eight checked items and the
remaining deployment task unchecked. These commits establish no further task
completion. An attempted PR-body update was rejected with `MCP tool call requires
approval, but approval policy is never`; no remote update was applied.

Added two CLI regressions using real collector observations on disposable inputs.
Each starts with twelve legitimate refusals across both lanes in three segments,
then replaces one test archive with a directory or symlink. The audit must exit 1,
report UNKNOWN coverage and unmet acceptance, and retain all eight readable
refusals with their multiplicity. The linked archive must not be followed. The
tests also verify byte-identical readable ledgers and receipt, plus unchanged
replacement directory or link. Production segments are never changed by these tests.

Validation exits zero:

```sh
PYTHON=/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3 node tests/test_audit_provision_ledger.js
node --check tests/test_audit_provision_ledger.js
git diff --check
```

`ledger-tests.log` retains all 55 passing tests. No Python or workflow changes
were made. Completion still requires access to the effective writer and collector
paths and the delivered installed generation after a fresh existing hourly tick;
every retained legitimate refusal must remain unmet acceptance.

The required source/test commit could not be created: staging failed with
`fatal: Unable to create '/home/runner/work/Orchestrator/Orchestrator/.git/index.lock': Read-only file system`
(`commit-attempt.log`). Changes remain in the working tree on `orch/issue-1363`.
The GitHub connector also rejected the blocker comment and `needs-human` label
with `MCP tool call requires approval, but approval policy is never`; neither
mutation was applied. PR #1365 was read back as open and ready for review.
