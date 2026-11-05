# Storage abort reconciliation

`storage_lifecycle.py reconcile-abort` is a one-use recovery surface for an
interrupted verifier that was proven stopped before installation. For an interrupted run
that never reached a receipt (any parent, including a pre-sync verify), use the automatic
`release-interrupted` drain described in `docs/STORAGE_LIFECYCLE.md` instead. It does not
retry verification, install a source, delete the owned root, claim recovered
bytes, update calibration, or mark a release as passing.

The request is an owner-private regular JSON file with this exact shape:

```json
{
  "schema": "storage-abort-reconciliation-request/v1",
  "caller": "original-human-task-reconciler",
  "state_dir": "/absolute/private/storage-ledger",
  "job": "alphanumeric-job-id",
  "expected_row_sha256": "...",
  "abort_proof": {"path": "/absolute/abort-proof.json", "sha256": "..."},
  "failed_release": {"path": "/absolute/release-final.json", "sha256": "..."},
  "candidate_sha": "40-hex-source-commit",
  "incumbent_token": "exact-incumbent-token",
  "owner_process_sha256": "sha256-of-the-abort-proof-owner-command",
  "child_process_sha256": "sha256-of-the-abort-proof-child-command",
  "release_owner_process_sha256": "sha256-of-the-abort-proof-release-owner-command"
}
```

The original storage producer must have sealed an owner-private
`<job>.stage-source.json` when it staged the child and pinned that record's hash
in the reservation. Reconciliation cross-checks this original-run record against
the row's source identity, identity command, root/token, owner PID and child PGID,
then joins the recorded child argv and owner `--source` to the abort process tree.
The v2 stage record also seals the original release parent process/tree, exact argv
and its typed `--expected-source-sha` and `--journal` arguments. Reconciliation
requires that original parent invocation, its direct storage child, and the
request's exact failed journal; finding a SHA elsewhere in a command is insufficient.
The verifier identity remains distinct from the release candidate. Candidate SHAs
must be exactly 40 lowercase hexadecimal characters. Legacy rows without this
producer record are held; do not backfill or rewrite historical evidence.

First create an evidence-only plan, then apply exactly that plan:

```sh
/opt/anaconda3/bin/python3 -B src/storage_lifecycle.py reconcile-abort --request REQUEST.json
/opt/anaconda3/bin/python3 -B src/storage_lifecycle.py reconcile-abort --request REQUEST.json --apply --expected-plan-sha256 PLAN_SHA256
```

Both operations require the existing job owner lock nonblocking. They re-read
the request, the hash-pinned abort proof and failed release journal, the ledger
row, owner marker/token, root device/inode, and hash-pinned release-owner, storage-owner, and child command
records in the abort-proof process tree. This intentionally keeps the storage
child's source identity distinct from the release candidate: both are preserved
and the release candidate is bound to the abort proof and failed journal.
Every abort-proof process record has exactly `pid`, `ppid`, `pgid`, and
`command`; duplicate PIDs are refused. The release leader must have `pid ==
pgid`, the storage owner must be its direct child in that PGID, and the verifier
child must be the recorded `child_pgid` and direct child of that storage owner.
The source checks all recorded abort-proof
processes. The source also performs the existing independent `ps` and `lsof`
self-visibility probe. A changed row, live or uninspectable process, missing or
unsafe file, existing intent/known receipt, or any probe ambiguity is held.

Apply writes an immutable intent before the compare-and-swap terminal finish,
uses the expected row digest under the ledger lock, then writes an immutable
known receipt after durable receipt readback. It deliberately retains the root
and its diagnostics. An interrupted or ambiguous reconciliation is not retried,
reset, or repaired by editing ledger JSON.

The storage forecast also handles an atomic capabilities-ledger replacement
without treating its vanished temporary sibling as a lost durable input. A
whole state inventory is retried at most three times, only for the exact
`capabilities.json.<eight tempfile characters>.tmp` namespace after that path
has disappeared and the physical root identity remains unchanged. Stable
files are still counted, and other missing files, symlinks, or root replacement
remain failures. This addresses a reproduced race; it does not identify the
historical writer that produced a particular admission failure.
