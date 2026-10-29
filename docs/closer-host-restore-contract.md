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
after readback.  Any failure after the intent receipt is written, including a
failure before `rename`, leaves this command held.  This contract has no
reconciliation consumer: stop, preserve the append-only intent/known receipts,
inspect the wrapper and retained evidence, and route that evidence to an
external reviewed recovery procedure.  Do not delete, move, or edit receipts
to retry; a later invocation of this command remains held when either receipt
exists.  It never writes campaign state, native index, ledgers, or results,
and exposes no acceptance, retry, or UNKNOWN-reclassification power.
