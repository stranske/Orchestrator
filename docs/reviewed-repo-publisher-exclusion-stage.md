# Reviewed-repo publisher exclusion stage

This repository does not own the running reviewed-repo publisher.  The two
scripts below prepare and apply a narrowly scoped local-automation repair for
the owner-excluded repository boundary; they do not discover, route, or run the
controller while preparing a stage.

`scripts/stage_reviewed_repo_publisher_exclusion.py` reads a captured publisher
directory and writes a new, create-only stage.  It retains the old helper,
generates a successor helper whose filename is its SHA-256, and renders matching
`demand_router.py` and `accountability.py` payloads.  The recipe records every
canonical preimage digest and mode, including the unchanged `controller.py`.

The offline regression suite uses the immutable four-file capture in
`tests/fixtures/reviewed_repo_publisher_exclusion/`; it copies those bytes into
a temporary canonical layout and rewrites only the copied helper path.  Tests
therefore never depend on, import, or execute the live local publisher.

The staged behavior is fail-closed before any cached/dynamic API read or route:

- cached targets and cached `pr_target` locators for `stranske/Collab-Admin`
  are excluded;
- continuity refuses an excluded target before its `gh api graphql` call;
- closing-reference and body-derived parent references are omitted before
  accountability routing/counting;
- the error-path `unknown_due` report omits excluded cached accountability rows.

The stage does not rewrite historical state.  A later normal controller pass
may publish its ordinary fresh state, but preparing or applying this source
bundle never hand-edits `demand-state.json`.

## Authorized deployment procedure

Only an authorized local-automation owner may apply a reviewed stage.  Before
that point, inspect the generated `apply-recipe.json`, its payload hashes, and
the current controller ownership.  Then invoke:

```sh
/opt/anaconda3/bin/python3 -B scripts/apply_reviewed_repo_publisher_exclusion_stage.py \
  --stage <reviewed-stage-directory> --apply
```

The applier takes the existing controller `.demand.lock` non-blockingly and
refuses a cooperating live writer.  This is a cooperative lock, not a claim to
block arbitrary same-UID writers that ignore it.  It rechecks every regular,
non-symlink preimage's SHA-256 and mode under that lock.  It publishes the new
helper with exclusive create, then atomically replaces `accountability.py`, and replaces
`demand_router.py` last.  Each write is fsynced, and a preimage mismatch or an
existing successor helper is a hold—not a retry or overwrite.

No schedule, controller invocation, GitHub API call, inventory read, state
edit, or assignment publication belongs to either script.
