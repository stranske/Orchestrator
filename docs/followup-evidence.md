# Delayed original evidence

The existing hourly `orchestrate.sh --active` tick owns `followup-evidence`.
Before GitHub-auth and capability preflights, it runs
`python src/followup_evidence.py` locally. After authentication/admission it runs
`python src/followup_evidence.py --reconcile` against that same observation (flat path in the deployed
mirror), using the existing Python launcher. No additional scheduler, research
run, model call or writer takeover is involved. The native collector has its own
nonblocking writer lock and reports failure instead of silently stopping.

Runtime configuration is `~/.codex/orchestrator/followup-evidence/config.json`.
It names only the nine original `strategy434-20261006` attempts, their frozen
successful done-record hashes, original session bindings and exact native source
paths. The export inbox is named in `export_inbox`. These are host data, not
repository secrets or inferred replacement trials. Missing configuration fails
closed and prints the missing step.

Run a bounded real-input check with the repository-selected Python:

```sh
python src/followup_evidence.py --apply
```

The command prints `outcome2`, the retained branch-refusal count and `outcome3`
(the number of qualifying original cost exports). Full packets and continuity
cursors are in `followup-evidence/latest.json` and `provision-state.json`.
The early phase never uses credentials or the network. A refused credential
prevents reconciliation while leaving local collection intact.
Successful local observations are retained before a ready packet attempts network
publication, so an outage cannot discard them. Incomplete evidence makes no network calls. Never edit source events or old
receipts to manufacture a pass.

For #1098, accounting reads every active/archived `provision-events.ndjson*`
segment. It filters owner-excluded entries before parsing, counting or reporting,
preserves genuine refusal outcomes and does not deduplicate events. Continuity
starts at the first observation, checks every prior segment prefix with retained
multiplicity, and resets on a missing/rewritten segment, implementation change,
malformed data or observation gap exceeding two hours. Old event timestamps alone
cannot prove coverage. A complete seven-day window with zero branch refusals
revives the existing opener for the separate native/full-verifier/mirror gates;
it does not close the issue or waive those gates. The collector continues while
acceptance is incomplete (blocking 1, drainable 1).

For #434, original aggregate `session_cost`, context occupancy and zero ledger
placeholders remain insufficient. Each `*.receipt.json` in the inbox must use
`schema: original-attempt-usage/v1`, the exact `experiment_id`, `attempt_run_id`,
`provider`, `session_id`, `started_at`, `completed_at`, `terminal: true`,
`coverage: whole_attempt`, and `source_kind: provider_export` or
`native_terminal_usage`. `raw_source_path` must point to a separate JSON export
inside the inbox, whose bytes match `raw_source_sha256` and whose record exactly
matches the normalized identity, terminal, coverage, timing and `segments` fields.
Each segment needs a unique original `request_id`, served `model`, explicit
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` and
corresponding `*_usd_per_million`, plus `price_source_url`, `price_valid_from` and
`price_valid_until` covering the original execution date. Cache zeros must be
explicit, never filled for a missing field. Conflicting receipts remain UNKNOWN.

A validated export is a source candidate, not Brain admission or proof that a
self-authored file is an authentic provider bill. The existing opener must finish
source adjudication and the remaining frozen synthesis acceptance through normal
delivery gates. The collector never updates Brain, source policy or issue closure.
It detects changes to the exact retained native sources as well as inbox exports;
new authentic data can resume work without an owner prompt. It cannot manufacture
an export the provider no longer retains.

For either issue, the first ready packet is frozen before publication. A remote
unknown outcome retries by reading its original causal marker, even if a later
collection changes. Comment readback precedes removing `agents:paused`; confirmed
handoff is acknowledged durably. Later hourly runs reuse the acknowledgment
without repeated API reads or messages. Live claims and other writer locks remain
owned by the original lanes.

## Unpublished standard issue branches

Provisioning now resumes a standard `orch/issue-N` or `orchestrator/issue-N`
branch with unpublished commits by adding a checkout at its existing tip. It
never resets that ref. Git's atomic worktree admission and existing dirty/live
holder checks remain in force; a custom branch with ambiguous ownership still
refuses. New productive attempts are `reused/branch-resume`; historical
`failed/branch-exists` records retain their original outcome.

The real November10 Workflows#4612 snapshot at
`d89d9557c335481ca8f9d75be811d7c608f73254` reproduced the original refusal
against its actual GitHub-tracking base in an isolated local clone. Restoration
opened that exact tip in1.714s and retained the failed and successful events.
The actual target branch and its existing native opener were not changed.
