# Issue #1216: provenance boundary, 2026-11-11

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
