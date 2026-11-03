# Issue #1088: source binding audit and remaining evidence limits

Observation date: 2026-11-03, during this hosted runner session. Issue acceptance
remains incomplete. This observation is not an October 20 replay receipt and
does not establish an eligible production candidate, apply, or resolved stall.

## Historical evidence search

The [original issue discussion](https://github.com/stranske/Orchestrator/issues/582#issuecomment-6167058330)
reports a native 12/12 provenance audit and a later exact-merge mutation recheck.
The [PR #595 provenance comment](https://github.com/stranske/Orchestrator/pull/595#issuecomment-6166759133)
names the private binding receipt:

`/Users/teacher/.codex/automations/pd-workloop-resume/coordination-20261014T0548/independent-582-provenance.json`

The issue discussion also names:

`/Users/teacher/.codex/automations/imi-merge-verify-closer/work/20261014T1047Z/582-deliberate-recheck.json`

Neither file nor `~/.codex/orchestrator/redirect-shadow/shadow.jsonl` is available
in this runner. The committed issue-582 verification directories and the fetched
issue #582 / PR #595 discussions contain earlier replay and mutation evidence,
but no genuine October 20 execution receipt was recovered. The comments establish
that an owner reported a private audit; they do not supply independently
inspectable source-to-fixture bindings here. Historical provenance remains
unverified in this session. No source identities were reconstructed from fixtures.

## Read-only audit implementation

Run this on the owner's host with the original corpus or its retained snapshot:

```sh
node scripts/audit_redirect_stall_provenance.js /path/to/private/shadow.jsonl
```

An optional second argument selects the fixture directory from the revision being
audited. The auditor requires the exact twelve-file roster, matches unique source
reports by retained sweep fingerprint, compares every retained report fact, checks
the added policy mapping, and requires a one-to-one target anonymization. Duplicate,
missing, reused, changed, or inconsistently anonymized bindings fail. Additional
stalled source reports are counted but cannot substitute for a fixture's binding.

Output binds each fixture byte hash to a source line and entry hash under the
observed corpus byte hash. It omits raw source targets, report contents, and entry
identities. It never writes source, fixtures, state, or a receipt file. Success
means the supplied source and fixtures match; authenticating the supplied corpus
and comparing its hash with an original owner receipt remain owner responsibilities.

Every result uses the current observation timestamp and explicitly retains
`historical_replay_receipt: not_established`. It does not execute a replay or a
live action. A missing source exits 1 with `status: unavailable` and
`private_source_corpus_unavailable`, rather than inferring an empty candidate
population. This is the actual result against the expected corpus location here.

## Verification in this session

```sh
node --test --test-isolation=none tests/test_redirect_stall_provenance.js tests/test_redirect_stall_fixtures.js tests/test_redirect_sweep_shell_backend.js
```

16 passed, zero failures or skips: seven new auditor tests plus the nine existing
offline replay/sweep checks. Auditor tests use explicitly manufactured temporary
source inputs; those inputs are only controls for the implementation and establish
no historical provenance. They cover retained-fact mutations, both directions of
target anonymization, missing/duplicate/reused bindings, malformed input, missing
fixtures, source immutability, sanitized output, and CLI exit behavior. CLI tests
call the real entry point in-process because this runner restricts spawning Node
children. Direct shell invocation also confirmed the unavailable-source exit 1.
Both JavaScript syntax checks and `git diff --check` pass. No Python files changed.

Separately, a temporary `git archive` of merged revision
`8f596dc01e1ede350724c1b88bc36610380af79b` was evaluated with the repository-selected
Python 3.14 interpreter, `/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3`:

| Execution | Exit | Result |
| --- | --- | --- |
| Both required named pytest nodes, `-q -m "not slow"` | 1 | `No module named pytest`, before collection |
| Both focused pytest files, `-q -m "not slow"` | 1 | `No module named pytest`, before collection |
| `src/redirect_apply.py --replay-stalls tests/fixtures/redirect_stalls` | 0 | `authorized 12/12 with named agents (dry-run)` |

The nodes are
`tests/test_redirect_chain_gate.py::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine`
and
`tests/test_redirect_apply_prescreen.py::test_redirect_apply_daily_cap_refuses_second_apply`.
The focused files are `tests/test_redirect_chain_gate.py` and
`tests/test_redirect_apply_prescreen.py`. Installing pytest failed because the
package index hostname could not resolve; `/opt/anaconda3/bin/python3` is absent.
These results do not satisfy the focused-execution criterion. The temporary
archive was removed after evaluation.

## Outstanding acceptance

The first task remains open for recovery or owner qualification of the private
bindings and any historical October 20 receipt. The second remains open until
the required pytest execution succeeds on an identified merged revision.

This runner has no installed production state or owner cadence receipts. Armed
flag state, eligible candidates, refusal records, blocking/drainable counts, and
action lineage are unmeasured here; none is reported as zero. Demand counts and
the fixture population cannot establish production eligibility. The third and
fourth tasks require the owner host's actual observation and the existing bounded
cadence. No flag, daily cap, writer lock, routing threshold, or cadence was changed.
No live outcome or control comparison is claimed, and all operational acceptance
remains incomplete.
