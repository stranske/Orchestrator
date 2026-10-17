# Route weights export

`python3 src/route_weights_export.py` writes the local shadow artifact
`$ORCH_STATE_DIR/route-weights-export.json`. It never changes a Git branch by default.

The intended Workflows consumer may fetch:

`https://raw.githubusercontent.com/stranske/Orchestrator/exports/route-weights/config/route-weights.json`

The document is versioned and has this contract:

```json
{
  "schema": "orchestrator.route-weights/v1",
  "generated_at": "2026-09-03T00:00:00Z",
  "source_version": 61,
  "min_observations": 20,
  "task_types": {
    "implement": {
      "ranking": [{"agent": "codex", "posterior": 0.67, "n_obs": 42, "success_rate": 0.61}],
      "evidence_ok": true
    }
  },
  "reserve": {"implement": [{"agent": "claude", "posterior": 0.58, "n_obs": 31, "success_rate": 0.55}]},
  "capacity_schema": "orchestrator.route-weights-capacity/v1",
  "capacity": {
    "codex": {"state": "ok", "window": "5h", "observed_at": 1725321600}
  }
}
```

`capacity_schema` and `capacity` are additive under the v1 route-weights schema so the Workflows
consumer (`agent_delegation_policy.js`, which accepts only `orchestrator.route-weights/v1`) keeps
reading posteriors. The block has one entry per configured agent in `capacity.AGENTS`, with `state`
copied verbatim from that tick's `capacity.build()` snapshot (`ok`, `warn`, `shed`, or `unknown`),
the agent's configured `window`, and `observed_at` set to the capacity snapshot's `generated_at`
(not the export's `generated_at`). Publish change detection compares `state` and `window` but
ignores `observed_at` alone so an unchanged pool state does not republish. Token counts, costs, and
account identifiers are intentionally omitted. v1 routing keys are byte-identical with or without
the capacity block; `exploration.policy_version` is likewise unchanged when only capacity is added.

Only the latest `route_weights` version is read. The public `task_types` keys are the consumer
contract: `implement`, `review`, `testgen`, `mechanical`, `codemod`, and `cross_repo`; each is also
checked against `router.ROUTE_TABLE`. Rankings contain only rows with `n_obs >= min_observations`.
`evidence_ok` is false when no non-reserve row meets that threshold. `claude` is never in a public
ranking: it is a reserve seat and appears only under `reserve`, which a consumer must ignore.

The consumer must fail open: on an unavailable, malformed, schema-incompatible, stale, or
`evidence_ok: false` task entry, retain its own current static delegation policy. An empty ranking
is not a zero score or an instruction to choose a fallback agent.

Publishing is deliberately double-gated. `--publish` has no remote effect unless
`ORCH_ROUTE_WEIGHTS_PUBLISH=1`; then the exporter uses `provision.ensure_canonical` for
`stranske/Orchestrator`, commits only `config/route-weights.json` on `exports/route-weights`, and
pushes that branch. It never modifies `main`. A semantic no-op prints `unchanged` and creates no
commit. Disable its daily shadow cadence with `ORCH_DISABLE_STEPS=route-weights-export`.

The exporter uses `feedback.resolve_db_path()` and logs `db=<absolute path> source_version=<n>`
before writing the shadow artifact. Selection precedence is `--feedback-db`, `ORCH_FEEDBACK_DB`,
`ORCH_LOCAL_RUNTIME/feedback/orchestrator.db`, `ORCH_STATE_DIR/feedback/orchestrator.db`, then
`feedback.DB_PATH` (normally `~/.codex/orchestrator/feedback/orchestrator.db`). The learner binds
its `DB_PATH` through that same resolver at import; in-process private-store patches remain supported.
CLI `--state-dir` controls only the shadow output and local refusal diagnostics, never the evidence
source. Readers never create databases or migrate schema.

Before publication, `publish_guard()` absolutely rejects source version zero/invalid and all-false
task evidence, even with the override. Partial true-to-false/missing task regressions are compared
against the fetched publication, so a changed local shadow cannot hide a regression. They require
an explicit `--allow-evidence-regression`, regardless of elapsed time. No timed release is authorized
by source660. The local `route-weights-regression-hold.json` retains `first_seen`, `task_types` and
`reason` for diagnostics only; recovery or an explicit partial override clears it. A changed task
set restarts its timestamp. Pure validation without a state directory never touches a hold file.
Capacity-only changes with identical `task_types` publish with that evidence unchanged.

Refusals exit non-zero and leave the remote untouched; cadence logs the reason plus
`refused: regressing_tasks=<n>, drainable_in=infiniteh`. Partial refusals name the required override.
`--allow-evidence-regression` logs the deliberate partial downgrade and never bypasses absolute
refusals. Publish commits record `db=<resolved path> source_version=<n> by=<git user.name>`,
with `orchestrator-export` as the identity fallback. The local shadow remains available for diagnosis.

The additive `exploration` block carries the weekly mode, sampled challengers, and a
`policy_version` identifying the exported policy. `keepalive_outcomes.py` joins trusted runner
dispatch timestamps to the export branch's commit history and records that tag, mode, weight
version, and commit SHA in each ingested agent run's `routing_metadata`. It uses neither PR
creation/merge time nor the current local shadow export. Existing keepalive rows with missing
provenance are retried without changing their run IDs, timestamps, or outcome lineage.
At settlement, ingest refreshes the runner history to include rounds added while the PR was open.

Missing runner timestamps or export history leave policy attribution unknown. A PR with rounds
under multiple policies retains a `dispatch_policies` list and has no single `policy_version`.
These tags identify the export available at dispatch according to its commit timestamp; they do
not prove the consumer fetched it or made an exploration decision. Ingest does not infer an
`exploration` flag from a matching agent or challenger.
