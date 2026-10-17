# Source660 acceptance reconciliation

Scope: existing PR #663, original branch `codex/issue-660-route-export-guard`.
Admitted head: `3439483f60151bfefbdb7d2c41bc99807f305516`.
Frozen sibling: #661 `4411533b2e1394544d187eafe37ce3e24ef243c3`.
Frozen integration: #663 `68db0c8de59fa0076f2b55e8a624c8bf5376f456`.

The guarded parent assessment `923ee21e` was read in full, together with its independent
AST binding reproduction. Its full 13-path analysis is preserved in `parent-assessment.md`;
`path-bindings.json` binds every path to both frozen heads, the admitted head, and this repair.
The parent reproduction found state-only learner/reader equality on #661 and divergence on #663.
All 26 frozen-head/path presence or hash comparisons against the actual Git objects match the
parent snapshots. Across these 13 paths, admitted #663 differs from frozen #663 only in the
verification floor; the three production acceptance gaps were still present at admission.
The new process test now writes through actual `feedback._conn()` and exports that same isolated
SQLite store through real CLI argument parsing and document construction. Merely patching two
reader calls cannot satisfy it. No live Brain, live state or export branch is used by this proof.

Source660's actual issue body requires an explicit `--allow-evidence-regression` for published
true→false changes. PR comments asserting a strict superset or completed acceptance contradict
the reproduced DB split and aged-release behavior. No direct durable owner instruction authorizing
automatic aged release was found in the source/PR evidence. This repair follows the original
criterion and the current bounded instruction. CLI `--state-dir` remains output/diagnostics only,
as documented and implemented on #661. The existing resolver's caller state fallback remains
available to API callers; the CLI no longer passes its output destination as that fallback.

## Every payload path

| Path | Disposition after reconciliation |
|---|---|
| `.verify-floor.json` | Preserve admitted floor and every ceiling; add seven collected/pass expectations for the seven new cases. No floor decrease or skip exclusion. |
| `ARCHITECTURE.md` | Preserve export schema/capacity/heartbeat/provenance description; correct shared learner selection, output-only CLI, and explicit-override requirement. |
| `README.md` | Preserve #663 functionality documentation; correct those same three compatibility/acceptance statements. |
| `agents/codex-660.md` | Do not restore the absent bootstrap marker; it carries no executable behavior, test, or acceptance evidence. |
| `docs/ROUTE_WEIGHTS_EXPORT.md` | Restore #661 output-only semantics and no-create/no-migrate contract; retain #663 absolute guard, fetched comparison, diagnostics, capacity and provenance documentation. Replace unauthorized timed release with explicit partial override. |
| `orchestrate.sh` | Preserve full combined refusal-log visibility and daily publish switch/failure marking. #661 stderr-tail is covered by the stronger combined tick log contract; no verified stderr-only consumer was found. |
| `src/feedback.py` | Restore actual learner binding through the shared resolver under state-only import. Preserve explicit DB, DB env, runtime env, API state fallback, shared default and private DB_PATH patches, including the legacy spelling of explicit private-copy paths. Add real isolated writer selftest. No new store or read-side migration. |
| `src/route_weights_export.py` | Preserve fetched remote comparison, zero/all-false/invalid-version refusal even with override, URI-safe reads, early provenance, remote shape/JSON validation, heartbeat order, and commit identity. Restore output-only CLI and explicit partial override at any age. Retain safe hold timestamp/task diagnostics; recovery/explicit partial release clears them. |
| `tests/test_firing_regressions_section.py` | Preserve keyword-compatible publisher mocks and repeated unchanged-export invocation heartbeat tests unchanged. Actual build stderr provenance remains covered elsewhere. |
| `tests/test_route_weights_export.py` | Preserve all valid guard, real-local-remote, path precedence, URI, recovery, hold isolation, malformed remote, heartbeat and failure visibility cases. Change only timed-release and CLI-source expectations to the original source contract; fresh and aged partial loss refuse without flag, absolute cases remain refused under both flag settings. |
| `tests/test_route_weights_export_cli.js` | Preserve all eight process-level missing/empty/empty-table/below-threshold cases, both override settings, Git tripwire, literal special-character DB paths, DB byte checks and shadows unchanged. |
| `tests/test_route_weights_publication.py` | Preserve capacity/no-op publication and URI-character/read-only source cases unchanged; no #661 zero/all-false override defect restored. |
| `tests/test_route_weights_publish_cadence.py` | Preserve publication switch, cadence wiring and compatible mock coverage unchanged. Full combined refusal/failure harness remains in export tests. |

New `tests/test_source660_reconciliation.py` contributes seven cases: state-only fresh-process
learner/CLI equality; fresh/24h/240h explicit-only guard; output-only CLI with a private patched
default; and two real SQLite→CLI→local bare-remote publication cases (aged refusal/explicit release).
The DB/CLI process installs a subprocess tripwire; catalog probing is disabled in isolated runs.
The publication integration replaces only canonical-checkout discovery, capacity input and ledger
heartbeat, while using the actual DB reader, CLI, guard, Git fetch/show/commit/push and remote JSON.

Dedup: resolver, guard, cadence, heartbeat and hold diagnostics already exist; this extends their
wiring/compatibility rather than creating a capability. The historical dormancy inventory and
improvement-log accessor were read; no live capability ledger or improvement log was modified.
The required advisor consult was attempted in task-specific words and failed on a ledger lock
permission error; it is advisory and fail-open, with raw stderr retained.

## Verification boundaries

`raw/` contains portable sanitized logs/XML, mutation argv/source bindings and the verification
result. `manifest.json` records original raw and sanitized hashes separately. The guard bypass
and learner DB disconnection each fail a named unchanged regression and pass after byte-identical
source restoration in an isolated source copy. Previous failed attempts remain in the archive.

These are local source proofs, not hosted current-head CI, merge authority, mirror propagation,
provider PASS, source acceptance, or postmerge live publication. #661 remains open and preserved.
The parent root guardian, dispatcher hold and parent `agents:paused` are retained; the parent alone
consumes this terminal receipt and restores its own pause. No merge, route-weight publication,
new branch/PR, automation setting or pre/postrun is performed.

## Final local result

- Targeted export/publication/cadence/heartbeat/DB suite plus the unchanged private-state compatibility
  control: **111 passed, 8 subtests passed**. Node CLI: **8 passed**. Both changed-module selftests pass;
  feedback names two sections unavailable without isolated ledger registration.
- Whole-tree Black, configured Ruff and mypy for both production modules pass. Supplemental C901
  retains the admitted source's 11 existing diagnostics and introduces no new violating symbol.
- Required unweakened `src/verify.py`: **RED**, exit 1: **3949 collected, 3901 passed,
  24 failed, 24 skipped**. The exact 24 failing cases reproduce on unchanged admitted
  source. Selftests `adapters`, `dispatcher`, `tick_watchdog` also reproduce their failures there.
  Causes are sandbox temp/process access, disabled/unavailable credential probes, and missing
  isolated Keychains prerequisites. Assertions, floors and skip ceilings were not relaxed.
- The first full run's additional private-state path-spelling failure was introduced by the initial
  resolver binding, caught, and repaired by retaining legacy explicit-path spelling. Its unchanged
  test now passes. Both full-run logs and XML, all failed intermediate attempts, and exact admitted
  source failure replays remain in the proof. The replay is a failure-subset comparison, not a
  baseline full-suite claim.
- Final production guard bypass and learner DB disconnection fail the named unchanged regressions;
  both pass after byte-identical restoration. `mutation-final-bindings.json` binds final source.

The local RED result remains an unresolved acceptance gate. Clean hosted current-head verification
and the original postmerge mirror/live-publication criterion remain for the parent/closer.

## Replay the portable proof

`executed-mutation-driver.py.txt` is the sanitized exact driver used for the final mutation run.
Replace `REPOSITORY_ROOT` with this commit's checkout and `EVIDENCE_ROOT` with a fresh scratch
directory, save it as Python, and run with the repository-selected interpreter. It copies source
and tests before changing anything, runs each unchanged named control, breaks actual production
wiring in the copy, restores original bytes in `finally`, and reruns green. Source hashes and exact
argv are in `raw/mutation-final-bindings.json`. `LOCAL_HOME`, `SYSTEM_TEMP` and `local-host` in logs
are redactions; they are not execution prerequisites. All publication tests use local bare remotes.
