# Native wrapper installer

This source-owned installer is a single CAS transition between a verified host
restore and later readmission. It validates sealed retained reconciliation,
renders only from the exact reviewed canonical preimage, records intent before
rename, and never creates a campaign or launches native entry.

The order is restoration → wrapper installation → model-free qualification →
readmission → business preflight → normal launch. A future wrapper hash cannot
pass the native contract before its bytes are installed. The installed wrapper
therefore refuses normal closer entry until the exact new campaign exists,
and always refuses legacy fallback when native opt-in is missing. An explicit
qualification request still enters the existing model-free qualification path.

Use `src/closer_native_wrapper_install.py plan` with `--reconciliation` (a saved
`{path, sha256}` reference), `--old-manifest`, `--old-state`, `--worker`,
`--new-manifest`, `--new-state`, and `--restore-receipt`. The new manifest must
pin the source-rendered preview bytes. `apply` takes the same inputs plus the
returned `--expected-plan-sha256`. Neither command creates a campaign or launches
native entry. The new campaign namespace must be empty at installation.

Existing old/new native, campaign, custody, canonical-installer and lane locks
remain part of the CAS guard, including the predecessor's dynamic lock closure.
Receipts live under the new observation root, so installation does not alter
the retained reconciliation's historical witnesses. Any failure after its
intent receipt is written, including before `rename`, blocks retry. There is no
automatic reconciliation handler: preserve those receipts and route them to an
external reviewed recovery procedure rather than deleting, moving, or editing
them. This version accepts only the explicitly reviewed restored predecessor.
