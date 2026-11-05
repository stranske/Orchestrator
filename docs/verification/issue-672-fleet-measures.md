# Fleet delivery measures — 2026-11-05 runner check

Implemented `fleet_delivery_measures()` and production CLI collection in
`src/switch_review.py`. JSON and readable output share one snapshot. Each reader
fails independently; one unreadable repository prevents a partial population from
being reported as a complete measurement. PR and issue reads are cached for this
report only. The source uses backlog's agent-label and source-issue rules and
excludes Collab-Admin from the new readers.

The canary reader fetches the authoritative Workflows
[`config/consumer_sync_canaries.json`](https://github.com/stranske/Workflows/blob/main/config/consumer_sync_canaries.json).
It measures all open `sync/workflows-*` PRs, including drafts and later pages.
Latency reports the median of observed PR/issue pairs and names still-open issues
and issues closed before merge. Released flow reports 48-hour totals divided by
two days, using current approval labels.

Verified in this run:

- `PYTHONPATH=src python3 -m unittest discover -s tests -p test_switch_review_fleet.py -q`:
  25 tests passed. These unittest tests are also collectable by pytest.
- `python3 src/switch_review.py --selftest`: passed.
- Deliberately wrapped the Gate read so an error returned a measured `0%`.
  `test_failed_gate_read_is_not_a_number` failed with `AssertionError: '0%' is not None`.
  Restored the reader and the same test passed. The mutation is absent from the source.
- CLI smoke check with writable temporary `ORCH_LOCAL_RUNTIME` and `ORCH_STATE_DIR`
  printed all five rows to `/tmp/orch-fleet-live.txt`, with explicit GitHub
  connectivity errors and an explicit missing opener-helper result. The report
  recorded `2026-11-05T00:22:36+00:00`, independently of the historical campaign target.
- `git diff --check`: passed.

Still incomplete:

- Gate REST run data identifies `pull_request`, not its `labeled`/`synchronize`
  action. The reader names the required run-ID-keyed action/conclusion join;
  it does not infer causes from actors, titles, or nearby label events.
- The opener helper is absent on this runner. Its documented three count fields
  are supported; the machine-local cap source remains unknown. A successful count
  response explicitly labels the cap unmeasured rather than assuming a limit.
- The requested `/opt/anaconda3/bin/python3` is absent and no repository-selected
  virtual environment was found. Smoke checks used the available Python 3.14.9,
  with private temporary runtime paths; this is not live-machine acceptance.
- `pytest` and `black` are absent, and shell network/DNS restrictions prevented
  installing them. Both requested commands were attempted and could not run.
  The required Black gate therefore blocks committing Python changes.
- A full `python3 src/verify.py` attempt was interrupted without a verdict.
  `python3 src/verify.py --pytest-shard 1/6` exited 1 with `No module named pytest`.
  Full verification and collection-floor reconciliation remain required. No
  collection or pass floor was changed without a measured full-suite collection.

Fixtures distinguish recorded REST/config projections and the observed shell
GitHub error from synthetic measurement examples. Offline fixtures establish
reader behavior, not production campaign measurements. No live acceptance or
full-suite task is marked complete.
