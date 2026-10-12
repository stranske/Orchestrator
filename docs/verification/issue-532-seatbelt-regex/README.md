# Issue 532: Seatbelt regex literal repair

## Original artifact validation and pre-change CI discovery (2026-10-12)

`verifyOriginalCI` in `scripts/verify_seatbelt_receipts.js` now reads the retained original
ZIP with the standard-library ZIP/XML parsers, verifies CRCs and recorded hashes, checks
original run/head bindings, and compares every extracted boundary case with its original
full-run record. It requires the complete twelve-case roster, including no-boundary and
fail-closed regressions. Ten new tests reject corrupted, incomplete, relabeled or altered
receipts, including altered extractions with recomputed hashes.

Verified original measurements: **2,717 passed, 26 skipped, 2,743 actual case records**.
The authentic JUnit summary reports 2,849 tests; the checker exposes that discrepancy and
uses case records corroborated by the retained job log. All twelve boundary cases passed.
A separate original focused-only command transcript remains **UNKNOWN**.

```sh
node tests/test_seatbelt_original_ci.js
node tests/test_seatbelt_hosted_ci.js
node tests/test_seatbelt_receipts.js
node scripts/verify_seatbelt_receipts.js
```

All 30 receipt tests pass. The four existing production quoting regressions pass via
stdlib unittest; adapters and dispatcher selftests pass. The existing native receipt
still verifies four production RED failures, byte-identical restoration and five macOS
GREEN probes against unchanged source/test bytes. This Linux execution establishes no
new native acceptance. Pytest and Black are unavailable locally; no Python files changed.

[3793ac8-ci-discovery.json](3793ac8-ci-discovery.json) retains existing connector metadata
and selected authentic job-log lines for pre-change head
`3793ac82ebd7feea3500f11bb30d34869007a297`. Checkout CI measured 2,863 passes and 31 skips;
the disposable flat-copy CI measured 2,842 passes and 52 skips, both from 2,894 collected
cases with 97 selftests. Gate's Python job was skipped. The connector exposes first-page
job discovery without total-count/attempt fields, so this snapshot does not establish
complete exact-head topology. PR #542 was confirmed open with `draft=false`.

The fourth task remains incomplete: the new checker commit needs its own CI evidence.
Exact-head review/floor gates and actual post-merge comparison remain pending. No workflow
or real agent was rerun, and no merge, manual mirror installation, research deployment or
whole-system acceptance is claimed. The sections below preserve historical observations.

## Current-head CI recovery (2026-10-12)

[380c160-hosted-ci.json](380c160-hosted-ci.json) retains authentic GitHub REST workflow/job
metadata and selected checkout/measurement log lines for head
`380c16044a15ba9a0438f845eadc4a57bb8e8266`, tested as synthetic merge `3bbe083`.
Both CI and Gate executed product checks: checkout verifier 2,863 passed / 31 prerequisite
skips / 2,894 collected, 97 selftests run; flat-mirror verifier 2,842 passed / 52 prerequisite
skips / 2,894 collected; Python 3.12 and 3.13 each 2,863 passed / 31 skipped.
Combined measurement, Black, Ruff and mypy steps also succeeded. The complete job population
includes the expected main-only reconcile and Docker skips; it is not an all-skipped rollup.

The repo-specific receipt checker now rejects wrong heads/attempts, incomplete discovery,
missing matrix members, skipped product steps, failed jobs and absent/invalid measured counts:

```sh
node tests/test_seatbelt_hosted_ci.js
node tests/test_seatbelt_receipts.js
node scripts/verify_seatbelt_receipts.js \
  docs/verification/issue-532-seatbelt-regex/380c160-hosted-ci.json \
  380c16044a15ba9a0438f845eadc4a57bb8e8266
```

Local validation: 13 hosted-receipt and seven native-receipt checks pass, as do the four
quoting regressions and adapters/dispatcher selftests. The native checker still binds four
production RED failures and byte-identical restoration GREEN (five real macOS probes) to
the unchanged production/test bytes. This Linux run reports new native acceptance UNKNOWN.
Local pytest is absent; its measured runner reports zero collected and exit 1. Installing
pytest/Black into `/tmp` failed, and the full local verifier attempt was interrupted without
a verdict. No Python files changed in this round.

This snapshot accepts only the recorded head. The receipt-checker commit requires its own
hosted CI. Review/floor guards and actual post-merge comparison remain pending; neither
remaining task checkbox is completed. No agents, merge, mirror installation or deployment
were performed. The older notes below remain historical evidence.

The candidate first regex-escapes an allowed root, then quotes the entire pattern with
_seatbelt_quote. The rule sequence remains protected denies, allowed subpaths, then .git
denies. The admission and argv code, and the inherited offload tests, are unchanged.

Local implementation progress (uncommitted; required Black gate is blocked):

- [x] Correct string escaping while preserving rule order and decoded regex semantics.
- [x] Add and execute production-generated regressions for quotes, backslashes, regex
  metacharacters, and their combination with spaces.
- [x] Retain native acceptance as UNKNOWN until authentic macOS evidence exists.
- [ ] Recover the original focused transcript as well as the original complete CI evidence.

## Candidate verification

The four new tests can run with the standard library, without starting any agent:

    PYTHONPATH=src python -m unittest discover -s tests -p test_seatbelt_regex_quoting.py -v

All four pass on this Linux runner. These check the generated literal's string layer, decoded
regex semantics, root isolation, direct/nested .git matches, ordinary source paths and
.git-other nonmatches, and the complete rule order. JSON decoding models the quoted string
layer only; Python regex matching is not native Seatbelt enforcement.

The same tests also execute the production profile through actual /usr/bin/sandbox-exec when
running on supported macOS. That probe creates and overwrites permitted source files, rejects
creation and overwrite of protected/sibling files and direct/nested .git files, and checks
the resulting files from the parent. All paths are inside a disposable directory. On this
Linux runner that branch did not execute: **native parser/enforcement acceptance is UNKNOWN**.

The deliberate control used the actual inherited src/adapters.py, confirmed byte-identical
to both local HEAD 5ec9327d239c97afdeb5787a1a154fa04c1d12f9 and merge
2465397667427d2f3e84f422adbbf461e88eb7ea. All four tests failed on that source. The candidate
was restored in a finally block, its bytes were compared to the saved candidate, and a fresh
subprocess ran all four tests successfully.

- Original adapters SHA256: 7441823895ab9c9f243f17f915eac370f8a45f940dc227f1bc4a740980335e21.
- Candidate adapters SHA256: a7f4d828df30b6d74b9ac401ce59686912b08bbe8711d6cce8fb6c56402caa0f.
- Receipts: [original RED](original-red.log), [restored GREEN](restored-green.log).

Both src/adapters.py --selftest and src/dispatcher.py --selftest passed. Compilation and
git diff --check passed. The four executed tests raise the executed-node union floor from
2743 to 2747 with every ceiling unchanged; exact pytest collection is still pending.

Required checks remain blocked:

    python -m pytest tests/test_offload_write_boundary.py tests/test_seatbelt_regex_quoting.py -m "not slow" -q
    black --line-length 100 src/adapters.py tests/test_seatbelt_regex_quoting.py
    black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .

Neither pytest nor Black is installed. An isolated /tmp virtual environment's installation
attempt failed because pypi.org could not resolve. There is no candidate full-suite,
formatting, exact-head review/floor-gate or post-merge acceptance claim, and no commit or push
is permitted until the mandatory Black checks pass.

Attempts to add a blocker comment and the needs-human label to PR 533 were rejected by
the GitHub connector: approval is required but this run's approval policy is never.
The PR was re-read afterward and remains open with draft=false at its original head.

## Recovered inherited evidence

[CI job 114580816630](https://github.com/stranske/Orchestrator/actions/runs/38176808788/job/114580816630)
records 2743 collected, 2717 passed, zero failed, 26 prerequisite skips, 97 successful selftests,
seven skipped selftests, three successful capability gates and two skipped gates.
[Gate Python 3.12 job 114581351588](https://github.com/stranske/Orchestrator/actions/runs/38176809124/job/114581351588)
also records 2717 passed and 26 skipped.

Both logs identify the tested synthetic merge as ca0b19c, merging original head
2c4509df9b53a32b4eb4143d7ba396808e38b49a into f6d3f9c3bd2a4c73fac68e680456eafaa15dcb32.
They are original CI evidence for inherited PR 524, not a full run of this candidate.
Selected authentic log lines and source bindings are retained in [inherited-ci.json](inherited-ci.json).
The focused transcript is still UNKNOWN; the artifact ZIP was returned as a remote file
reference and could not be inspected locally.

The inherited [actual post-merge comparison](https://github.com/stranske/Orchestrator/pull/524#issuecomment-6114368956)
is NON_PASS (CONCERNS/CONCERNS). It does not accept this correction.
No manual mirror installation, research-program deployment or whole native acceptance is claimed.
No real agent or verifier workflow was rerun.
