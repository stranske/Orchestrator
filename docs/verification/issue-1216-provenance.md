# Issue #1216 verification

## Local keepalive round 2, 2026-11-11

The named acceptance command using `/opt/anaconda3/bin/python3` exited 0
with **113 passed and 30 subtests passed**. The two changed test files add
13 cases to the previous collection and pass expectations. The full
suite runs in CI.

### Deliberate break and restoration

Both commands used the requested interpreter and `-m pytest -q --tb=short`.
After each run, `src/model_profile_trial.py` was restored byte for byte.
Restored SHA-256: `4a91202bfdff96916ce43557e236195bcb9157a9ab88ea6369203170c5f0fb12`.

| Test in `tests/test_profile_trial_ingest.py` | Mutation in `src/model_profile_trial.py` | Deliberate failure | Restored run |
| --- | --- | --- | --- |
| `test_ingest_is_one_transaction_and_a_mid_write_failure_leaves_nothing[_record_run_in_conn-2]` | Commit the first attempt with a separate feedback call before the normal trial transaction | Exit 1, `AssertionError: runs`, `assert 1 == 0` | Exit 0, 1 passed |
| `test_local_identity_claims_require_an_independent_provider_lookup` | Make `_authoritative_identity_verified` succeed when the independent lookup is absent | Exit 1, `Failed: DID NOT RAISE ValueError` | Exit 0, 1 passed |

### Review dispositions for PR #1193

Round 3 re-read the complete review-thread listing on 2026-11-11. All six
threads were unresolved and current on the closed predecessor PR. The
dispositions below describe their status on successor PR #1352.

| Thread | Disposition on this branch |
| --- | --- |
| [Independent response provenance](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493714910) | `_authoritative_identity_verified` requires a lookup supplied by trusted application code and compares the returned response ID, model, completion state, trial metadata and token usage. `test_local_identity_claims_require_an_independent_provider_lookup` rejects the forged local claim before feedback writes. The CLI fails closed because it has no trusted collector. |
| [Headingless specifications](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493714928) | `_recurring_work_rubric` uses the first two nonblank lines when there is no criteria block. `test_work_quality_checks_the_frozen_spec_task` gives `2:2,3:3` a score of `1.0` for a headingless spec with a leading blank, and gives literal criterion echoes `0.0`. |
| [Empty or repeated evidence](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493714945) | `_work_quality_score` requires a nonempty rubric and exactly one complete evidence line. `test_work_quality_checks_the_frozen_spec_task` gives an empty heading and empty answer `0.0`, rejects duplicate evidence, and covers multiline and inline criteria. |
| [Matching null usage](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493714963) | `test_identity_artifact_explicit_null_usage` proves matching nulls stay unmeasured and mismatches fail in both directions. `test_identity_artifact_tokens_must_agree_with_attempt` verifies that omitted artifact fields preserve attempt usage. |
| [Independent response ID rejection](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493714994) | `test_invalid_or_unconfirmed_response_ids_refuse_even_with_a_trusted_lookup` recomputes the artifact digest after removing or changing the ID and verifies all trial feedback tables remain empty. |
| [Session-only feedback](https://github.com/stranske/Orchestrator/pull/1193#discussion_r4493715009) | `test_publish_current_records_measured_usage_quality_and_identity` checks six distinct returned attempt IDs, six distinct persisted `execution_attempts`, and zero `profile_trial_ingests` rows with explicitly unverified session evidence. |

Round 3 repeated the acceptance command with `/opt/anaconda3/bin/python3`.
It exited 0 with **113 passed and 30 subtests passed**. For hosted CI, use the
[PR #1352 checks](https://github.com/stranske/Orchestrator/pull/1352/checks)
for the commit containing this record. Match the check run's head SHA to the
PR head before accepting its results.

### Live run-current record

From 20:35:43 to 20:36:55 UTC, one invocation of
`/opt/anaconda3/bin/python3 src/model_profile_trial.py run-current` used the
PR worktree as the Orchestrator source and the local `stranske__Workflows`
mirror at `86e74e7842183048c2fe1b9a5ad5fd304fbce70d`. Both source manifests
matched readback. The shared Codex pool was supplied as `warn` from the
20:29:58 UTC native capacity poll, which reported 87% weekly usage and
ordinary usage allowed. The runner was Codex CLI 0.167.0.

The deployed receipt for `strategy434-20261006` matched the archived receipt
byte for byte. The archived specification also appeared byte for byte in
three retained evaluation prompts. The original spec path was absent, so
the receipt and archived inputs were copied to an isolated directory. Only
the copied `strategy.json` path was made relative to its copied `spec.md`.
Outputs targeted that directory's `state/profile-trial-state.json`,
`state/capability-program/profile-trial.json`, quarantine database, and
`attempts/` directory.

Trial `model-profile-trial:b538078409df3bff6c8b478b` bound all six final
messages to the packet and frozen specification. Each final matched the
corresponding native session's completed turn, from which input and output
tokens were read. The rubric expected `1:21,3:8`.

| Ordinal | Requested profile | Instance | Input tokens | Output tokens | Rubric score |
| --- | --- | --- | --- | --- | --- |
| 1 | `codex-5.6-terra-high` | 3 | 14380 | 315 | 1.0 |
| 2 | `codex-6-astra-high` | 3 | 19584 | 260 | 1.0 |
| 3 | `codex-5.6-terra-high` | 1 | 18775 | 415 | 1.0 |
| 4 | `codex-5.6-terra-high` | 2 | 14489 | 402 | 1.0 |
| 5 | `codex-6-astra-high` | 1 | 19584 | 267 | 1.0 |
| 6 | `codex-6-astra-high` | 2 | 15298 | 278 | 1.0 |

**Provider identity and publication are incomplete.** The command exited 2
with `provider_resolved_identity_unavailable` after the six completions.
The CLI produced no identity artifacts, quarantine database, trial state
or new program summary. The existing deployed summary identified a
different three-instance trial. The per-attempt usage and quality above
come from the six saved sessions and final messages. They are grouped by
requested profile and carry no independently verified provider model.
The private evidence directory
`~/.codex/orchestrator/measurement/orch-1352-20261111/` holds the originals,
their hashes in `readback.json`, and the reconstructed manifest validated
against all six run IDs.

## Historical round 1 record, 2026-11-11

This record covers the source issue's narrowed provenance slice on
`codex/issue-1216`, based on `3987c448`. These changes are **uncommitted** because
this run mounts `.git` read-only. This is local implementation evidence, not a
claim that the full issue or final-head CI is complete.

- [x] Require independent provider provenance for verified ingestion while
  preserving explicitly unverified session measurements. Verified locally by
  the named regression tests below.
- [ ] Deliver the changes in a commit on the checked-out PR branch.
- [ ] Complete the remaining rubric, bridge, mutation-transcript, review,
  production-trial, and final-head CI tasks from the original issue.

## Implemented boundary

[`model_profile_trial.py`](../../src/model_profile_trial.py) accepts a
`provider_response_lookup` from trusted application code, separately from the
result JSON. The callback must authenticate provider provenance independently
of session transcripts and their local artifacts. Verification binds a unique,
valid response ID to a completed provider response, the model, trial run,
profile, frozen packet, and measured usage. Missing lookup or confirmation
refuses verified ingestion before any feedback writes. Authority names and
matching local digests alone confer no trust.

`results_from_codex_sessions` labels its measurements and identity artifacts
unverified. Publishing those measurements retains usage and quality in six
distinct attempt rows without a verified-ingest marker. The CLI has no trusted
provider collector; production identity verification remains **incomplete**.
No live provider execution or six-attempt production record is claimed.

## Validation

Pinned pytest 9.2.0 on Python 3.14.10:

```text
python3 -m pytest -q tests/test_profile_trial_ingest.py tests/test_profile_trial_run.py tests/test_profile_trial_recurring_spec.py tests/test_model_profile_trial_bridge.py tests/test_profile_trial_instances.py -m "not slow"
100 passed, 30 subtests passed; exit 0
```

`test_forged_session_provenance_cannot_enable_verified_ingest` exercises both
`response.completed` and `server_model` claims with forged verification flags
and recomputed artifact digests. A verified-ingest request leaves all seven
trial feedback tables empty. Explicit unverified recording then retains six
distinct attempts, measured usage, and quality, with zero ingest markers.

`test_local_identity_claims_require_an_independent_provider_lookup`,
`test_invalid_or_unconfirmed_response_ids_refuse_even_with_a_trusted_lookup`,
and `test_provider_response_must_independently_confirm_the_trial_measurement`
cover missing/invalid/forged IDs and independent response mismatches. Existing
successful-ingest tests now supply a separate fake authenticated lookup; they
are offline fixtures, not production provenance evidence.

Additional checks:

- `node --test tests/test_profile_trial_cli.js`: exit 0 on Python 3.14.10.
- `python3 src/model_profile_trial.py --selftest`: exit 0.
- Related summary/model-profile tests: 25 passed on Python 3.12.16 with an
  isolated writable `ORCH_STATE_DIR`; this was a supplemental compatibility run.
- Pinned Black 26.10.1 formatted the changed Python files and passed
  `black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`:
  699 files unchanged, exit 0. Sandbox worker restrictions required checking
  files individually first; the final unmodified CLI invocation passed using
  that formatter cache. The earlier Black 25.1.0 result is not acceptance evidence.
- Pinned pytest collection: 8,611 cases. Baseline copies of the five trial files
  collect 84 cases; the updated files contain 100. `.verify-floor.json` therefore
  adds 16 to the collection and expected-pass counts, preserving every ceiling.
  A full-suite passing run is not claimed.
- `git diff --check`: exit 0.

The requested `/opt/anaconda3/bin/python3` does not exist here. Test tools were
assembled under `/tmp` from pinned upstream sources through the GitHub connector
because direct package installation could not resolve PyPI. Repository
dependency files were not changed. Black source tag: `26.10.1`; pytest source tag:
`9.2.0`.

## Delivery blockers

```text
git add src/model_profile_trial.py ...
fatal: Unable to create '.../.git/index.lock': Read-only file system
```

PR #1352 is open and ready (`draft=false`). Adding `needs-human` and posting a
blocker comment were each refused by the GitHub tool with:
`MCP tool call requires approval, but approval policy is never`.
Neither remote mutation occurred. A run permitting writes to the existing
branch's `.git` is required to commit these reviewed working-tree changes;
the PR label/comment also require an environment permitting those mutations.
