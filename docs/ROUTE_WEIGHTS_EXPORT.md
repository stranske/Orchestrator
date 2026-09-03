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
  "reserve": {"implement": [{"agent": "claude", "posterior": 0.58, "n_obs": 31, "success_rate": 0.55}]}
}
```

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
