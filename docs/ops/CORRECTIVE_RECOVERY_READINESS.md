# Corrective recovery and native Verify readiness

This contour uses the existing published `aead261f` controller, publication ledger,
queue, protected discovery, fresh-root reader, and native artifact readback. It
does not introduce a scheduler, publication writer, or recovery of an old UNKNOWN
operation. The source support table remains empty until actual consumer delivery
is verified.

## Released source versus deployed support

Workflows source `27bb99216d0602f10fe13f541ecc8db1980a53f3` and its approved
five-file source-delta plan can establish candidate bytes, not deployment.
The compiled plan is `sha256:edc7627763381bc8be3c70e47fb168f45b28a754af165d43580f3ab862898aba`,
with five entries, no removals and no dependencies. The sole rollout owner retains
consumer-write custody. Core does not create a competing rollout.

Before adding a support-table row, retain all of:

- Consumer repository identity and canonical merged consumer revision; exact
  `.github/workflows/agents-verifier.yml` bytes and mode at that revision.
- Receipt joining the approved plan/source to actual delivered target bytes and
  modes at that consumer revision, including the unchanged accepted baseline.
- Effective reusable workflow revision and content, independently of the helper
  checkout. The latter is authenticated again from the eventual native checkout
  job log; moving `main` or a reusable SHA does not substitute for it.
- Normative helper fingerprints for reusable workflow, corrective selector
  parser, verification event, context collector, semantic validator and terminal
  collector. `SOURCE_FILES` names these exact files. A valid Git blob with changed
  selector or validator bytes must remain unsupported.
- Independent source/receipt acceptance. The candidate inventory alone never
  grants a POST, issue close, model call, or consumer registration.

The retained local candidate inventory is
`/tmp/closer-area3-deployed-source-pin-readiness.json`. It explicitly records
`deployed=false`. It must not be copied directly into `DEPLOYED_SOURCE_PINS`.

## One fresh bounded interruption/reentry proof

Coordinate the existing publication owner and use one newly created synthetic
namespace, inert Git/API effect ports, and the exact composed source package.
No existing native UNKNOWN namespace is an input. The coding-success input is a
retained immutable typed terminal fixture, not a new provider call or a claimed
installed worker positive. Use the existing real queue/ledger/custody harness;
external effects remain mocked.

1. Retain a coding-success handoff with the original request, raw findings,
   typed tests and owned PR/head identity. Create the one publication intent
   under the original queue frame and writer.
2. Inject ACK loss after the mock push begins, separately from failure of the
   native readback. Preserve the original intent and `unknown` ledger state.
   Reenter the queue with a fresh observation. Assert the push count stays one,
   no second commit/push is issued, no confirmed result is fabricated, and the
   original operation remains unresolved. A matching remote head alone cannot
   confirm or reopen that publication.
3. In a separate known-completion fixture, retain the existing confirmed
   publication result and native-origin signature. A fresh root discovers the
   protected hint through `observe_existing_root`. The hint grants planning
   data only. `attach_existing_root` must obtain its one-shot view from the real
   five-object reader in the new `execute_bound`/queue frame and new writer.
   Copied JSON, changed request/tests, foreign HMAC/index or changed native head
   must hold. No old IO execution capability is revived.
4. For Verify, preserve the original Tasks/AC and all authenticated typed tests.
   With deployed support absent, hold before dispatch. Once legitimately pinned,
   read the exact native comment/run/attempt/jobs/upload-window/archive. Require
   manifest/event recomputation and compact-row equality. Wrong attempt, expired
   archive, changed checkout or missing scope remains UNKNOWN/held. Verify uses
   existing API routes; this is distinct from coding subscription transport.
5. Exercise existing hold follow-up tick: UNKNOWN gets observation/escalation,
   never an execution retry. Only authenticated `confirmed_not_started` release
   can rearm to fresh PREPARE after current source, role and capacity checks.
   An accepted Verify request waits for its original bound native terminal;
   readback cannot manufacture an outer POST receipt.

The bounded source-only run may use the already present tests:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH="$PWD/src" python -m pytest -q \
  tests/test_closer_publication_io_backend_mechanics.py \
  tests/test_closer_publication_admitted_reader.py \
  tests/test_closer_publication_verification.py \
  tests/test_closer_corrective_verifier_readback.py \
  tests/test_closer_hold_followup.py \
  tests/test_closer_verification_ack_barrier.py \
  tests/test_closer_terminal_read_recovery.py
```

Use a canonical physical checkout path and a fresh pytest namespace. Allow a
180-second outer source-control budget; observed baseline six-file run was
77.46 seconds. This test command authorizes no remote/provider effect. A separate
native assembled proof still needs genuine source installation, admission,
original queue/writer and current consumer receipts; mocked ports are not that
evidence. There is no deployment or closure claim from these controls.

## Existing hold-tick activation boundary

`closer_campaign.tick` already invokes hold observation/rearm through the native
observer. `closer_hold_followup` distinguishes `confirmed_not_started` from
`provider_outcome_unknown` and `action_outcome_unknown`; release never changes
that distinction. Activation requires the selected installed controller to load
this reviewed source and its actual current campaign/frontier/release policy.
Do not repoint the live legacy wrapper or use a private TEST admission as business
effect authority. First real corrective canary closure covers only actual
native-linked sources; a distinct original source without native closing linkage
remains held/undone despite being included in verification scope.
