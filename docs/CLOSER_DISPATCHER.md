# Status-driven closer dispatcher

`src/closer_dispatcher.py` is the local closer's coordinator. `src/closer_queue.py`
retains item and operation state and rejects a premature round stop. It consumes
the existing controller assignment; it does not own schedules, models, campaign
budgets or intake. It operates through existing claims, GitHub capacity controls,
private offloads, exact-head merge guard and verifier evidence reader.

## Run and integrate

Inventory is read-only: no provider execution, claims or queue writes. It still
uses the existing GitHub rate ledger when reading live data.

```sh
python3 src/closer_dispatcher.py run --assignment /path/to/closer-assignment.json \
  --state /path/to/closer-queue.json
```

Execute an authorized round with `--apply --workers`. Two private workers may run
concurrently by default. The worker timeout is 120 seconds, bounded further by
remaining round time and any existing campaign permit. The maximum active round
is 2700 seconds, plus five minutes reserved for receipts. Every assignment receives an observation, action, current evidence,
revision, owner, operation or a concrete deferral and next trigger. A waiting item
never ends work on an independent eligible item.

The same queue path resumes interrupted work without resetting its deadline.
Completed results survive restarts. Pending private results retain `requires_coordinator`;
collecting them does not authorize a valid stop while senior integration remains
and actual usable time is available. The trusted coordinator acknowledges the exact
`integrated_operation` with current reviewer/head/review/source bindings, then
continues the actual next action. An acknowledgment cannot satisfy acceptance gates. Unknown mutation outcomes remain parked until
reconciliation proves what happened; they are never retried merely because a
command returned an error. A pass bound prevents a zero-progress spin, but does
not authorize a valid stop while actionable work and usable time remain. An
expired recovery records its overrun honestly and launches no new actions.

Canonical workspaces are resolved only for assigned repositories and validated
against their Git remote without networking. `--workspaces` overrides this with
a JSON mapping of target or repository to a local checkout. A mechanical repair
requires a clean exact-head checkout; the existing offload then copies it into an
isolated local workspace. No worker edits a shared checkout or publishes results.

Luna Low handles read-only evidence extraction, review and repair diagnosis.
A senior-integrated mechanical repair plan may use the existing Terra Medium
profile. Profile requests and actual worker results are retained separately;
configuration is not proof that a requested model was served. Unsupported-model,
authentication or quota failures stop further affected worker launches and remain
visible. There is no silent model fallback or increased spend cap. Luna and Terra
share the Codex subscription pool. The existing capacity report and authoritative
shed marker are checked without reading agent sessions. Registered repair roots
also pass the existing assessment campaign admission check; this coordinator
cannot authorize, renew or extend their permits.

## Action and acceptance boundaries

| Evidence state | Action |
| --- | --- |
| Source issue has one explicit delivery PR | Follow that PR while retaining issue and PR aliases |
| Source linkage is missing or ambiguous | Collect evidence for senior integration |
| Review findings remain | Private review, then an integrated response/thread disposition |
| Current checks fail or branch conflicts | Bounded diagnosis; an explicit current-head mechanical plan permits isolated repair |
| Tasks/AC evidence is incomplete | Collect evidence; green CI does not authorize merge |
| Complete current-head Tasks/AC evidence | Invoke the existing exact-head merge guard |
| Merged with no existing verifier request/result | Add the existing compare label once after deduplication |
| Verifier running, history incomplete or outcome uncertain | Retain operation/run evidence and continue other targets |
| Verifier NON_PASS | Collect/adjudicate findings; never close source automatically |
| Merge-bound PASS and accepted original source evidence | Call the existing supervisor source-closure actor |

`--dispositions` is a **trusted coordinator input**, never a private-worker output
file. Each target's acceptance disposition names `reviewer`, exact `head`, current
`review_revision`, `source_digest` from `source_digest(snapshot["sources"])`, and
`criteria`. Coverage includes the complete original source body plus every
nonblank Tasks/Acceptance/Success Criteria line, including unbulleted prose. Each
row records `source`, literal `criterion`, `result: "PASS"` and nonempty `evidence`:

```json
{"path": "/retained/actual-test-or-observation.txt", "sha256": "actual-file-sha256"}
```

The coordinator must verify these artifacts actually support each requirement.
Hashes establish retained bytes, not semantic truth or reviewer identity. Missing
coverage, modified artifacts, changed source text, new review state or moved heads
invalidate the disposition. Sources whose requirements cannot be mapped safely
remain with the senior closer. Worker reports and PR checkboxes cannot supply
merge/closure authority.

An integrated review response additionally names a hashed `response` file and,
when justified, `resolve_threads` containing current thread IDs. A mechanical
`repair` plan names `head`, a concrete relative-file `paths` list and a bounded
`prompt`; without that plan the worker only diagnoses. Existing campaign permits,
where required, are supplied as `campaign_permits`; they are checked, never issued.

The existing merge guard re-reads live review/check state and pins the mutation
with `--match-head-commit`. Server-side required reviews/checks remain necessary:
a new review can arrive without moving the head. The dispatcher cannot make that
GitHub race atomic by caching a ready status.

Verification deduplication checks existing labels, trusted merge-bound receipts,
active runs and relevant recent history. Truncated or unattributable history is
unknown, not permission to spend again. This actor does not remove/re-add verifier
labels or rerun providers on unchanged evidence. A retry needs an independently
justified source-owned disposition.

Source closure composes `keepalive_supervisor.close_verified_issues` from the
separately owned PR #701/source #675. It preserves `ORCH_SUPERVISOR_CLOSE`; it does not
enable that actor or copy unmerged code. Until the actor is merged, deployed and
enabled through its normal ownership path, source closure records that dependency.
Its comments, refreshed identities/holds, confirmed external result and Brain
outcome remain owned by that actor. Guard and closure subprocess groups are bounded
by the round deadline; a timeout stays uncertain for reconciliation.

## Deployment and acceptance

Use the existing guarded Orchestrator mirror sync after normal repository delivery.
Then preview `python3 scripts/install_closer_dispatcher.py`. The explicit `--apply`
updates only canonical closer prompt text and the postrun gate, preserving all
other TOML values. Generated prompts use existing renderers. Strict `--enforce-stop`
requires existing old-prompt rounds to be terminal; no schedule or running writer
is changed. Backups and compare checks protect concurrent local edits.

The ACTION C environment must retain `CLOSER_ROUND_STATE`. Postrun independently
validates accounting and assignment binding before recording end-round or relaying.
A rejected stop leaves the ledger intact and releases the normal lane mutex.

Code tests, merged delivery, deployed hashes and observed live transitions are
separate acceptance claims. Offline concurrency tests demonstrate scheduler behavior;
a live private-worker result does not demonstrate a real guarded merge or source
closure. Keep those boundaries explicit in the operational receipt.
