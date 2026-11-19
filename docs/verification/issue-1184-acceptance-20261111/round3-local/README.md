# Local operational readback — 2026-11-11

Required outcome, still unverified: a native verifier-corpus-decision/v1 binding
PR 1168 head 01e86ae7e4ef0e6a06193c916c8c780ca5cab052 to published merge
a30b34ada04a30d04360867e14097a8c5b6d731c, with authentic worker/publication custody.
Command: `/opt/anaconda3/bin/python3 tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py --chain-evidence`
Check date: 2026-11-11. This command currently targets PR 989; its success alone
does not establish the PR 1168 outcome.
Stop rule: three failed attempts at the same step or 45 minutes.

## Verified result

The remaining outcome is **incomplete**: T5, T6, A3 and A4 remain unchecked.
No implementation failure was printed; this commit retains evidence only.

- Corrected contract: exit 0, `contract ok`.
- Isolated exporter extracted with `git show` from partial revision
  `0507d28acf68d6bf21c68138d18d56cd01968827`: exit 1 with
  `AssertionError: CSV contract is not implemented`.
- Focused regression: both tests pass using `/opt/anaconda3/bin/python3`.
- Current chain command: exit 0. Full stdout/stderr and command statuses are
  retained here. Its receipt and native publication snapshot are retained too.
  They prove the reader's PR **989** result and grant no acceptance authority.
- Complete entry-point inspection reconfirms explicit default `run_contract`
  assertions and evidence readers behind `--chain-evidence` (with `--legacy`
  selecting the historical reader). Focused regression verifies no default
  subprocess/network evidence collection. Fixture-only pytest collection exits
  5 with no tests collected, as intended. No collection override changes this.
- Native PR readback reconfirms #1168 and #989 merged with the recorded final
  heads and published merge SHAs. No merge is applicable and no historical head
  was changed. Both PRs retain visible `test` and `agents:keepalive` labels.

## First broken step: native verifier completion

The complete paginated native #1168 comment read contains no trusted
`verifier-corpus-decision/v1` for its final head and published merge. The three
retained native attempts remain incomplete; success of a workflow is not a
corpus decision. Skipped runs and #984 events are excluded.

A native recovery dispatch selecting evaluation run 42141057834, attempt 1,
artifact 13468410186 was refused **locally with exit 77**, before network
mutation. Full refusal text is retained. The dispatcher-owned gh guard says
production workflows must be started by their schedules or controller and
explicitly instructs this seat to leave such a run undone. No alternative
transport was used to bypass it.

Blocking/drainable pair for this step: **1 blocking / 0 drainable by this
dispatched seat**. The controller's authorized production workflow route is the
continuation owner; it must reconcile retained evaluation through the existing
native workflow before any new provider evaluation. No new hold was created.

## Worker/publication custody

`retain-confirmed-result.txt` is the historical producer function extracted
from the revision in `source-manifest.json`. It requires actual publication
execution, ACK/readback consistency and canonical index binding before storing
`publication-result-<sha256>.json`; its provenance copies
`scope.history["terminal_reference"]` to `coding_terminal_reference`.

The retained original #1168 Codex log has successful pushes and exact native
readbacks for `d750240291d2b5c455d64258e011d6f4fd68a7b3` and
`860c9125a1f0e6a467c806a092951f3952219bcb`. The exact native command records
are retained, with source-log hashes and terminal lines. The log contains no
occurrence of final head `01e86ae7e4ef0e6a06193c916c8c780ca5cab052`.
The later #983 Cursor log starts after merge and explicitly reports no push;
its mention of that head cannot establish production of it.

No stored historical publication-result or corresponding terminal reference
for the final #1168 corrective head was located in the inspected local custody
and prior acceptance bundles. This is a bounded evidence gap, not a claim that
no record exists anywhere. Commit authorship and a merge acknowledgment cannot
replace those records. An earlier-started worker is acceptable if its genuine
terminal/publication records trace the produced final commit; no redundant
worker was started and no records were fabricated.

## Delivery

Only this evidence directory changes. Existing fixture and test labeling remain.
The local source head before this evidence-only update is
`edbc933c20de87eccdf68614813ccb2b0b528a33`. T1–T4, T7, A1, A2, A5 and A6
are verified locally; the remaining four ids are not completed.
Post-push review belongs to the next scheduled local keepalive after the
seven-minute floor; this automation does not wait on CI or merge PR 1348.
