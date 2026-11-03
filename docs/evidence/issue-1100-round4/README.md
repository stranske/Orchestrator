# Issue #1100: raw relay receipt correlation

Operational acceptance remains **unproved**. Production Brain and relay logs are absent on
this runner, so production counts remain unknown. The new tests use synthetic private state.
No production receiver was restarted, hook replayed, or lock released.

## Task reconciliation

Reviewed the changes in `efaa78c`, `96cbd07`, and `7608bd6`. Their review record and passing
CLI witnesses support completing only the source-review task:

- [x] Read the complete merged rate_incidents.py, legacy matcher and focused tests to finish the review omitted from this input.
- [ ] Correlate existing production decisions, completed exits, persisted rounds, and accepted edges.
- [ ] Report cumulative and newly attributable production joins for the same window.
- [ ] Obtain fresh production switch-review text output for that window.
- [ ] Obtain passing repository tests and the requested verifier terminal result.
- [ ] Adjudicate the original deadline from attributable production timestamps.

All operational acceptance criteria remain unchecked. Updating both duplicate review checkboxes
in the remote PR body was rejected by the connector. The reconciled body is retained at
`/tmp/issue1100-reconciled-pr-body.md`. The blocker-comment and `needs-human` label requests
were also rejected; [action results](remote-update-results.json) retain the reason.

## Implementation

Extended `scripts/report_relay_join_window.js` with paired optional `--pacing-log` and
`--exit-log` arguments. The original persisted-only counts remain separate from a new
`raw_log_correlation` population. This advances the blocked correlation/reporting tasks
without changing production ingestion or claiming a relay implementation defect.

The raw audit enumerates explicit decisions from the pacing log, including decisions never
imported into Brain. For each decision it retains original pacing/exit lines, line numbers,
timestamps, raw decision ID, persisted round facts, and accepted edge IDs. File byte counts
and SHA-256 hashes identify the log reads. An accepted raw join requires agreement between
the raw pacing facts, raw completed exit, persisted source decision, persisted round, and
accepted relay outcome edge. Failed completed rounds remain eligible for outcome attribution.
Conflicting identities, missing persistence, and disagreeing receipt facts remain visible.

The caller supplies inclusive start/end Unix seconds. A completion after the end cannot
establish a timely accepted join. Exact repeated receipts are retained without counting a
second decision. Missing inputs yield unavailable/null counts; malformed evidence yields an
incomplete report with line diagnostics and counts for only the valid subset. The CLI exits
nonzero for unavailable or incomplete raw evidence. Availability is not an acceptance verdict.

Example for an operator with production read access, supplying the actual observation end:

```text
node scripts/report_relay_join_window.js --db /path/to/orchestrator.db --start 1793693792 --end END_UNIX_SECONDS --pacing-log /path/to/relay-pacing.log --exit-log /path/to/relay-exits.log
```

Brain is opened read-only; raw logs are read without importing or rewriting them. Brain and
file reads are separate snapshots, so concurrent production writes may require a later
observation. Legacy adjacency, host deployment provenance, log completeness, dry-run exclusion,
fresh switch-review counts, and original-deadline acceptance still require operator assessment.

## Validation and terminal results

`node --test --test-isolation=none tests/test_relay_acceptance_cli.js`: **16 passed**, exit 0
([output](relay-cli-tests.txt), [bounded execution record](relay-cli-tests.json)). Five new tests
cover unimported decisions with persisted rounds, exact raw attribution and later recovery,
receipt/round mismatches, conflicting pacing facts, and unavailable/malformed evidence.
They exercise the real CLI, assert nonzero exits for incomplete evidence, and verify that
repeated auditing preserves the fixture Brain and raw-log bytes.

Both changed JavaScript files pass `node --check`; `git diff --check` passes
([validation](validation.json)). No Python source
was changed, so the Python-only Black pre-commit gate does not apply.

Required repository/verifier/report attempts remain unsuccessful:

- Repository pytest, bounded to 60 seconds and using `-m "not slow"`: exit 1, pytest absent
  ([output](repository-pytest.txt), [record](repository-pytest.json)). No cached pytest package
  was found in the installed Python toolchains; the system interpreter also lacks pytest.
- `/opt/anaconda3/bin/python3 src/verify.py`: exit 127, interpreter absent
  ([output](requested-verify.txt), [record](requested-verify.json)).
- Available-interpreter verifier with private state: timed out at 45 seconds, exit 124, no
  program output ([output](available-verify.txt), [record](available-verify.json)). No passing
  verifier result or cause of the timeout is inferred.
- Requested-interpreter `src/switch_review.py --text-out ...`: exit 127, interpreter absent
  ([output](requested-switch-review.txt), [record](requested-switch-review.json)). No fresh
  production report was produced. No speculative switch-review repair was made.

The bounded read-only [production-path audit](production-window.txt) returned exit 1 and
unavailable/null counts because Brain is absent ([record](production-window.json)). It is an
environment failure receipt, not measured production traffic. The [environment readback](environment.json)
also confirms missing production pacing/exit logs and no relevant path overrides.
Read-only retrieval of the issue #1100 and PR #1101 discussions supplied no new production receipts.

## Floor, deadline, and delivery

`.verify-floor.json` is unchanged and byte-identical to assessed local `origin/main`
`296e144bbe2a7c847329d3fee879a0e995fd0a33`. Both SHA-256 values are
`780186ee0caf07002daac988ecbd91c4d58efb8e1957adb164c8ebae25d0f849`.
The freshly retrieved GitHub main floor blob also matches local blob
`3ac9a5d4ce7b5854ae9cf8b8a7a2d23a4ba0a7f9`
([comparison](source-and-floor.json)). This floor check does not establish test success.

The original deadline is **2026-11-04T08:16:32Z**, still in the future at this observation.
Without attributable production evidence, neither timely acceptance nor a missed deadline
can be established. Later recovery must remain separate from the original window.

Staging the tested source/test files failed with exit 128 because `.git` is read-only
([output](git-add.txt), [record](git-add.json)). No commit or push was possible. The source,
tests, and evidence remain in the working tree. GitHub updates were rejected with
`MCP tool call requires approval, but approval policy is never`; the remote task body,
comment thread, and labels were not changed by this run. The final read-only
[PR metadata](pr-state.json) shows PR #1101 was merged externally at **2026-11-03T21:51:42Z**,
using prior head `7608bd6e7db577e12a668c8042cbb7f0f4c78447` and merge commit
`8ad3fd93293f389fa1207e8f2b9597e77ce15c5e`; it is closed with `isDraft=false`.
This run did not request that merge. These uncommitted audit changes were not included,
and the external merge does not establish the outstanding operational acceptance criteria.

Completion requires a production-readable seat with the requested interpreter/test tools,
writable Git metadata, and an authorized GitHub mutation channel.
