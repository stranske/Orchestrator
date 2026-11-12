# Runtime directory aliases in the ledger audit

The remaining deployment task stays unchecked. This runner has neither the
deployment runtime nor the installed mirror generation. The attempted audit of
the latest recorded host paths exits 1 with ENOENT (`deployment-audit.log`).
No deployment, production collection, or live acceptance is established. The
latest retained host audit still reports 188 refusals and incomplete coverage;
it is historical evidence, not a fresh observation of this delivery.

The auditor rejected intact collector observations when the writer, collector,
or CLI used different directory aliases for the same runtime. It compared
absolute path spellings rather than resolving directory aliases. The fix
resolves parent directories for writer and receipt segment comparisons, and
resolves the CLI runtime directory. It does not resolve final ledger components:
symlinked ledger files still fail the regular-file check. Missing receipt paths
fail comparison without suppressing the readable ledger's refusal counts.

Two new regressions exercise both CLI/runtime and writer path spellings against
real collector observations made through a directory alias on disposable data.
The empty observation qualifies only ledger evidence; four duplicate legitimate
refusals across both lanes and retained segments remain unmet. Both cases verify
continuous coverage, CLI status, and byte-identical receipt and ledger inputs.
These test observations are not production collector observations.

`ledger-before.log` retains the two new failures and 51 passing existing cases.
After the fix, `ledger-tests.log` retains all 53 passing cases, including the
existing symlinked-ledger and unrelated-writer protections. Command:

```sh
PYTHON=/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3 node tests/test_audit_provision_ledger.js
```

Both JavaScript syntax checks and `git diff --check` pass. No Python, workflow,
verification floor, live collector state, holder, lock, or retained production
segment was changed. Completing the unchecked task requires the deployment
host's effective writer path, installed generation, and a fresh observation
from its existing hourly collector; every retained legitimate refusal must
remain unmet acceptance.

The required source/test commit was attempted on the checked-out
`orch/issue-1363` branch. Staging failed before any commit could be created:

```text
fatal: Unable to create '/home/runner/work/Orchestrator/Orchestrator/.git/index.lock': Read-only file system
```

The GitHub connector rejected both the blocker comment and the `needs-human`
label with `MCP tool call requires approval, but approval policy is never`.
Those remote updates were not applied. Source and tests remain uncommitted in
the working tree. PR 1365 was read back as open and ready for review.
