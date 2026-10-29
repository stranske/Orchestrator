# Host-only closer wrapper restore

`scripts/closer_host_restore.py` implements `closer-host-restore-contract/v1`.
It is a one-file CAS operation for `$HOME/.codex/bin/cursor-lane.sh`, not a
native-round recovery.  Its request pins only the retained swap intent, known
receipt, backup, signed admission/handoff/terminal records, and raw UNKNOWN
worker result.  The canonical wrapper, lock closure, runtime module closure,
and receipt destination are derived from those witnesses; callers cannot
supply them.

Planning is read-only.  Apply requires the exact plan digest, acquires the
derived nonblocking locks, rescans witnesses under lock, writes and fsyncs an
intent receipt before `rename`, and writes an immutable known receipt only
after readback.  A failure after rename is intentionally unretryable without
reconciliation.  It never writes campaign state, native index, ledgers, or
results, and exposes no acceptance, retry, or UNKNOWN-reclassification power.
