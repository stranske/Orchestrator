# Issue 1394: remote exploration provenance break-and-revert

Deliberate mutation: remove `tick.remote_tick`'s forwarding of router pick fields into
`delegate_kwargs["routing"]` (the block immediately after `influenced_by_role_run_ids`).

## Named sentinel under break

```sh
python3 -m pytest tests/test_exploration_review_sentinel.py::test_remote_exploratory_pick_reaches_exploration_review -q
```

- Exit code: **1**
- Failure: `exploration_review._recorded_exploration_evidence()` reports `exploration_runs == 0`
  (expected 1) because `delegate_remote` received no routing metadata and recorded no
  exploration-flagged dispatch.

## Restored source, full file

After restoring `src/tick.py` bytes:

```sh
python3 -m pytest tests/test_exploration_review_sentinel.py -q
```

- Exit code: **0**
- **3 passed** (includes `test_remote_exploratory_pick_reaches_exploration_review` with
  `unjoined_exploration_evidence.count == 1`).

Executed 2026-11-16 on PR branch `cursor/issue-1394` (head after `8390f07`).