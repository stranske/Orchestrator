# Collector observation lifecycle regressions

Reconciliation reviewed `7b7a72e8`, `bffb40ef`, and `e7010e4b`. The latter
two commits added tests and runner-local evidence without completing deployment
acceptance. The PR already has its first three task boxes checked. No additional
task or acceptance box is complete in this round.

The first unchecked task remains blocked by the selected interpreter's missing
pytest package. The selected Python is
`/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`; the other installed Python
versions also lack pytest and Black. No Python source or floor file was changed.
The deployed generation and hourly collector runtime are absent at the standard
host paths. These tests provide no deployment or seven-day host acceptance.

`verify.log` retains the complete output of the following command, with exit 1
recorded in `verify.exit`:

```sh
ORCH_STATE_DIR=/tmp/orch-1363-round3/state \
ORCH_LOCAL_RUNTIME=/tmp/orch-1363-round3/runtime \
PYTEST_ADDOPTS='-m "not slow"' python3 src/verify.py
```

It collects zero pytest cases, runs 103 of 111 module selftests,
and reports failures in `ccusage_reconcile`, `gh_capacity`, `local_verify`,
and the admission gate. The activation audit passes. The first named selftest
failure reproduces as an attempted write to the read-only
`/home/runner/.codex/handoff`. Both `ccusage_reconcile` and `gh_capacity` pass
unchanged when the existing `HANDOFF_DIR` override points under `/tmp`.
The admission reproduction fails on missing pytest; no capability-admission
or heartbeat implementation defect was established. Full reproduction outputs
and commands are retained in `failure-reproductions.log`. The full verifier was
not rerun after correcting the handoff path because pytest remains unavailable.

`tests/test_audit_provision_ledger.js` now carries the real collector's previous
observation into one subsequent hourly observation. Two new regressions verify:

- A refusal at the seven-day boundary remains counted against a still-fresh
  receipt until the collector observes the next window. After that observation,
  the refusal ages out with all active/archive bytes retained and the continuous
  baseline preserved.
- Losing one of two identical archives resets the collector baseline, even when
  the surviving refusal has now aged out. A fresh receipt with zero refusals
  still cannot establish seven continuous observed days.

These use disposable state and the real `followup_evidence.provision_report`.
All 41 ledger-auditor tests and all 39 native-runner tests pass. Native-runner
regressions simulate pytest; they do not execute the actual provisioning suites.
`validation.log` retains complete output and statuses, including two isolated
mutation checks. Each new test passes against its unchanged implementation and
fails at the intended assertion after the corresponding window or segment
multiplicity protection is removed. Mutations were confined to temporary copies.
Node syntax and `git diff --check` pass.

`floor-comparison.log` retains the exact tested revision and byte comparison
against `origin/main`; `cmp` exits zero. The tested checkout is
`e7010e4bb4044d6a9420f9f65ce41ecc0afb987b`, with the JavaScript test changes
described here in its working tree.

`blockers.log` retains the missing-pytest reproduction, deployment-path probe,
and failed staging attempt. Git cannot create `.git/index.lock` because `.git`
is mounted read-only, so this environment cannot create the requested commit.
The GitHub connector also rejects the reconciliation body update,
`needs-human` label, and blocker comment because they require approval under policy `never`.
PR #1365 was confirmed open with `draft=false`.
