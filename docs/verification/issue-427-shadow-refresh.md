# Existing-spec shadow refresh recovery

Merged #475 compare37332974148 remains NON_PASS. The production ingestion path
skipped observation whenever a remote run already existed, and ran observation
only after first spec authoring. The once-authored SHA also prevented later
exact PR heads from executing.

The existing ingest now calls a shared observation helper for new and existing
agent runs. Specs remain once-authored and byte-identical. Every observation
compares the actual checkout HEAD with the supplied current PR SHA, requires
existing execution switches, and remains shadow-only. Operator-owned specs and
dry runs retain their existing safety behavior. No merge gate or outcome is changed.

Actual controls on prior source:
```text
FAILED test_existing_ingested_run_refreshes_shadow_observation
FAILED test_existing_shadow_spec_observes_new_exact_head
2 failed in 0.93s
```
After repair:
```text
python3.12 -m pytest -q -o addopts= tests/test_runtime_ac_shadow.py
23 passed in 3.23s
```
The new-head control also deliberately supplies a different actual checkout SHA
and confirms it does not execute. Dry-run repeat never invokes the observer.
No live ingestion, Brain write, mirror publication, or flag activation was performed.
