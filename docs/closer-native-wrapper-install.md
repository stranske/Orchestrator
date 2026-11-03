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

For a newly signed `native-pre-dispatch-no-start/v2` finite refusal, use
`--kind prospective` instead. It takes the retained old manifest/state/worker,
the fresh manifest/state, and the host-restore receipt; it does not take (or
invent) a historical reconciliation reference. The receipt's hash-checked
archived `6ba…` backup is used only as the renderer base. Planning proves that
the *current* canonical wrapper is the exact deterministic render of that base
under the old manifest and that its pin matches; it then previews the fresh
manifest render from the same base. The old campaign/result/proof are only
read and remain unchanged. The new native observation root must contain only
its two pre-provisioned locks and the fresh campaign state must be absent.

For a source-controlled prospective manifest, the installer derives both the
old and new controller custody writer locks from each current manifest's
`state_path`, repository id, and epoch. A legacy restoration predecessor's
custody locks never substitute for those current fences. Its observation and
campaign roots are renderer witnesses too: their retired lock closure is not a
prospective lock requirement. After recording its intent and flushing the
replacement temporary file, the installer re-reads the wrapper preimage
immediately before `rename`; a change made by a cooperating writer holding the
same lock closure is refused and preserved. This is a cooperative Source
locking contract, not a claim of atomic protection from an arbitrary same-UID
writer that ignores those locks.

Prospective installation only performs the wrapper CAS. The supported recovery
sequence is: prospective wrapper `plan|apply`; ordinary model-free
qualification of the new manifest; `closer_native_readmission.py
prepare-prospective --receipt SIGNED_KNOWN --expected-plan-sha256 PLAN_SHA`; and
then the separate, fresh typed `admit` step. The post-CAS preparation command
accepts no caller-provided worker configuration or frontier: it authenticates
the new-source HMAC seal, intent, and known receipt, binds the exact pre-CAS
old manifest/state/result/no-start witnesses, and invokes only the new native
contract. It creates one source-initialization HELD campaign, not demand,
native work, publication, or acceptance. A bad/missing receipt or any changed
witness remains `held`; a valid v2 proof alone never permits retry.
