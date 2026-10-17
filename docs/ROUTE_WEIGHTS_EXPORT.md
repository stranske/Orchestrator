# Route weights export

`python3 src/route_weights_export.py` writes the local shadow artifact
`$ORCH_STATE_DIR/route-weights-export.json`. It never changes a Git branch by default.

The exporter and learner share `feedback.resolve_db_path()`: `--feedback-db` overrides
`ORCH_FEEDBACK_DB`, then the runtime root is selected from `ORCH_LOCAL_RUNTIME`,
`ORCH_STATE_DIR`, or `~/.codex/orchestrator` (in that order), with
`feedback/orchestrator.db` appended. `--state-dir` selects the shadow output directory only.
Every export run logs `db=<resolved path> source_version=<n>` before writing its shadow artifact.
Reading an absent or empty database does not create or migrate it.

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

Before writing the remote artifact, `publish_guard(new_doc, published_doc)` refuses publication
if the source version is zero, all task types have `evidence_ok: false`, or any task type with
published `evidence_ok: true` would lose that evidence (including a missing task entry). The
comparison uses the fetched export branch, not the local shadow artifact. A refusal exits
non-zero and prints `publish_guard refused: <reason>`; the cadence step surfaces that reason
in the tick log and records a failed attempt. The local shadow artifact remains available for
diagnosis. An owner-approved downgrade can use `--allow-evidence-regression` alongside the
existing publication switches; it logs `deliberate evidence regression allowed` with the reason.
The flag does not enable publication on its own.

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
