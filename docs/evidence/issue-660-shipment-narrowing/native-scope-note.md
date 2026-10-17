# Source660 explicit-home continuation on existing PR663

Round 5 scope disposition: the pre-sync helper change was dropped from PR663.
`scripts/verify_before_sync.sh` was restored to `origin/main`, and its companion
`tests/test_route_weights_presync_source.js` was removed. The preserved manifests
and receipts below describe the earlier snapshot, not the round 5 tree. Their
helper bindings do not establish current-head correctness of the pre-sync selector.

Admitted head: `43c2b4c5e551e05f4d10e27388991709ce35fffa`.
Original branch: `codex/issue-660-route-export-guard`.
Frozen sibling661: `4411533b2e1394544d187eafe37ce3e24ef243c3`.
This is a bounded continuation of the original source660 reconciliation, not a new campaign.

The actual guarded Sol7314a10e attempt2 result is preserved in `assessment.md`: **NEEDS-FIX**
on16d for explicit `ORCH_FEEDBACK_DB=~/private/orchestrator.db`. Its approval of other
components is advisory and is not approval of this head. Native39035257159 completed successfully
and delivered43c; the actual16d→43c diff contains only `scripts/verify_before_sync.sh` and
`tests/test_route_weights_presync_source.js`. Both native files were byte-identical at
that snapshot and are bound separately in the historical manifest. The real pre-sync script
and sync/deployment helpers were not run for that snapshot.

## Finding, repair and causal controls

The explicit-env initialization bypassed `resolve_db_path()`, leaving a literal relative `~/`
for the learner's actual `_conn()` while the exporter expanded HOME. The production repair adds
only `.expanduser()` to that branch. It does not canonicalize explicit paths: legacy `/tmp`
spelling remains available to the unchanged private-copy compatibility test. `_conn()` still
uses the module's `DB_PATH`, preserving in-process private-store monkeypatches.

The single new pytest case is
`tests/test_source660_reconciliation.py::test_explicit_tilde_learner_writer_and_cli_share_db`.
On unchanged admitted43c production source it fails with **AssertionError: explicit tilde learner
DB binding split**. The actual opened SQLite filename points into the private cwd's literal `~/`
instead of the owned temporary HOME. The same test bytes pass after the repair. It writes version79
through actual `_conn()`, runs real CLI parsing/document construction with no publish flag,
checks the selected file, version and evidence, verifies unchanged DB bytes, rejects an extra
literal tilde store, and installs a subprocess tripwire. Competing runtime/state overrides and the
CLI shadow directory cannot redirect the selected DB. Capacity and heartbeat use isolated stubs;
no live Brain/state/routes or provider subprocess is consulted. The module selftest also covers
home expansion and restores HOME and all saved selection variables.

Final-source isolated-copy mutations retain unchanged test bytes and genuine pytest XML:

| Production mutation | Unchanged named control | Result |
|---|---|---|
| Remove explicit-env `.expanduser()` | new explicit-tilde learner/CLI case | GREEN → assertion RED → byte-identical restored GREEN |
| Remove learner resolver binding | existing state-only learner/CLI case | GREEN → assertion RED → byte-identical restored GREEN |
| Make `publish_guard` always return true | existing default empty-DB CLI case | GREEN → provisioning-tripwire RED → byte-identical restored GREEN |

`mutate-final.py.txt` is the executed final driver; `raw/mutation-final-bindings.json` binds
actual argv, cwd, source/test hashes and exit codes. `run.py.txt` records the validation commands.
The first mutation driver's overly narrow expectation of AssertionError did not accept pytest's
intentional guard tripwire failure; its evidence is retained, including restoration in `finally`.
The final driver correctly recognizes that specific existing guard failure. No test was changed.

## Selection modes and compatibility

`precedence.py.txt` and `raw/precedence.json` record twelve fresh-process configurations without
opening a database: default HOME, state-only, runtime-only, runtime over state, tilde state,
tilde runtime, absolute DB, tilde DB, relative DB, `/tmp` alias DB, empty DB with runtime, and empty
DB without another override. Each compares physical learner/reader identity and tests explicit
API selection over all overrides. Runtime precedes caller state fallback, which precedes state env;
CLI `--state-dir` remains output-only. Existing resolver/CLI tests exercise those paths as well.
An empty explicit DB string without another override still selects cwd (an invalid DB target),
while with runtime it falls through to runtime. This pre-existing malformed-input edge was diagnosed,
not changed by this explicit-tilde continuation. No valid path split was found in the other modes.

The advisory consult used task-specific words and `--surface opener-lane`, against a private ledger
snapshot. It offered generated/non-dispatch-ready deliberate-break-verifier; direct causal controls
were executed without changing its activation gate. Other offers were withheld for missing PR facts.
The consult receipt preserves offers, declines, missing facts and error=null. No live ledger write.
Dedup finding: this repairs existing DB binding, resolver and regression infrastructure; no new
capability, ledger entry, workflow or schema was created.

## All thirteen original payload paths

| Path | Current disposition |
|---|---|
| `.verify-floor.json` | Observed collection3950; add exactly one case to prior3949 collection/pass expectations. Every ceiling unchanged. Passed3950 is an expectation, not an observed full-suite result. |
| `ARCHITECTURE.md` | Preserve prior restored shared binding/output-only/explicit-override documentation byte-for-byte. |
| `README.md` | Preserve prior reconciled contract byte-for-byte. |
| `agents/codex-660.md` | Preserve recorded absence: bootstrap-only marker, no executable or acceptance payload to restore. |
| `docs/ROUTE_WEIGHTS_EXPORT.md` | Preserve restored output-only/no-create/no-migrate contract and stronger safety/provenance documentation byte-for-byte. |
| `orchestrate.sh` | Preserve full combined refusal visibility, daily switch and failure marking byte-for-byte. |
| `src/feedback.py` | Expand explicit home syntax; preserve prior state binding, precedence, legacy alias spelling and private DB_PATH patches. |
| `src/route_weights_export.py` | Preserve indefinite partial refusal/explicit override, absolute zero/all-false/invalid-version refusal, fetched comparison, URI-safe read-only access, early provenance, malformed-remote refusal and heartbeat order byte-for-byte. |
| `tests/test_firing_regressions_section.py` | Preserve heartbeat and keyword-compatible mock coverage byte-for-byte. |
| `tests/test_route_weights_export.py` | Preserve all guard/CLI/hold/recovery/fetched-remote/diagnostic controls byte-for-byte. |
| `tests/test_route_weights_export_cli.js` | Preserve eight process controls, special-character DB paths, tripwire/read-only/shadow assertions byte-for-byte. |
| `tests/test_route_weights_publication.py` | Preserve capacity/no-op/URI/read-only tests byte-for-byte. |
| `tests/test_route_weights_publish_cadence.py` | Preserve cadence/switch/mock coverage byte-for-byte. |

The manifest binds all13 paths to frozen661, admitted43c and current source, all10 original validation
source paths to current bytes, and the two native43c paths separately. `old-component-bindings.json`
retains and verifies **all52 old bindings** against Git objects, including the prior reconciled16d
state. Every file in `docs/evidence/issue-660-reconciliation/20261017-source-contract` remains
byte-identical to43c; that archive is an immutable historical component, not current execution proof.
All pre-existing reconciliation test bytes are unchanged after removing only the new function.

## Actual validation and limits

- Final targeted suite, including unchanged private-copy compatibility: **112 passed, 8 subtests passed**.
- Unchanged Node CLI controls: **8 passed**, explicit Anaconda interpreter and canonical `/private/tmp`.
- Both module selftests pass. Whole-tree Black and configured Ruff pass; mypy passes for both source modules.
- Collection-only: **3950 collected**. No test/assertion/exclusion/skip ceiling was weakened.
- Full local verifier was not repeated: CLAUDE.md requires targeted tests/selftests for a PR change of this
  kind, with full local verification reserved for capability ledger/lifecycle/binding and skipped-only
  surfaces. This change touches none of those surfaces. The prior unweakened full run remains **RED**:
  **3949 collected, 3901 passed, 24 failed, 24 skipped**, with the same24 failure identities reproduced
  on admitted source, plus `adapters`, `dispatcher`, `tick_watchdog` selftest failures. A subset replay
  does not establish baseline full-suite PASS. Current targeted GREEN does not turn that result green.

All failed intermediate attempts remain in `raw/`. The initial selftest addition compared alias
spellings and failed; its new physical-identity assertion was corrected while retaining the exact
expanded spelling assertion. The first unchanged Node run under `/tmp` also failed its diagnostic
path string assertion because the exporter prints canonical `/private/tmp`; rerunning with canonical
TMPDIR passes all eight, with unchanged tests and production source. These are visible harness
limitations, not hidden passing results.

Fresh full PR/source comments, all-head reservations/completions, direct recorded attempts and
main workflow_dispatch attempts were checked before checkout and again before commit/push. All recorded attempts, their actual admitted child jobs, and every job step are terminal.
The new sibling661 attempt39036750577 completed/failure with Run Codex skipped and an error
completion (no output captured); it establishes terminal non-execution, not delivery. Its head
remains frozen. No pending parent or absent/queued admitted child was accepted as terminal.
The expected source660 guardian, this dispatcher663 claim and parent pause remain. Ownership evidence
and hashes are retained in `admission/`. An absent/pending admitted writer would park the exact target.

Current-head hosted CI is asynchronous and remains UNKNOWN until observed on the pushed head; there
is no CI wait in this continuation. No sibling/source closure, merge, live publication, Brain write,
settings change, workflow edit, schema change, guardian release, parent-pause removal, or pre/postrun
was performed. Both661 and663 remain preserved. Full acceptance, clean hosted verification and original
postmerge mirror/live-publication criteria remain for the original owner. A prepared parent handoff
records the pushed head and these limits separately.
