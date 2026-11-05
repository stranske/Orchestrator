# Closer launch operator

Use the source-owned operator only after the normal release/install gates:

```sh
/opt/anaconda3/bin/python3 -B src/closer_launch_operator.py plan \
  --manifest /absolute/closer-runtime-manifest.json --campaign /absolute/campaign.json \
  --run-id operator-record-1
```

`plan` is read-only (except the explicitly requested conditional capacity-owner
refresh). It never swaps the wrapper, clears a campaign, creates an admission,
or qualifies a model. A `ready` diagnostic is not authority.

The execution form requires a matching run identity before planning or capacity
refresh. It invokes `/bin/bash <canonical-wrapper> closer`, preserving the
native entry's OS parent identity and repeating its fresh guards:

```sh
/opt/anaconda3/bin/python3 -B src/closer_launch_operator.py launch \
  --manifest /absolute/closer-runtime-manifest.json --campaign /absolute/campaign.json \
  --run-id operator-record-1 \
  --confirm-run-id operator-record-1
```

For retained evidence, do not use a green release/CI result as coding or
acceptance evidence. Emit a conservative ledger instead:

```sh
/opt/anaconda3/bin/python3 -B src/closer_launch_operator.py ledger \
  --campaign /absolute/campaign.json --result /absolute/worker-result.json \
  --native-terminal /absolute/native-terminal.json
```

Missing coding, publication, verification, or acceptance evidence remains
`UNKNOWN`; the ledger names the last proven boundary.

The owned Codex stdio transport retains native command and file-change events
as JSONL in the existing result output. Command records carry native thread,
turn, and item identity, the command, exit status, and available native output;
agent messages remain separate escaped text. The coding terminal's result
digest and publication handoff bind this projection. Existing capture and
retention limits still apply; malformed or oversized evidence is refused.
Review the actual command records and published file bytes for required
failure/pass sequences. A final agent message or successful process exit does
not establish those task-specific results.

For a finite frontier, an unavailable terminal worker is not retry authority.
The plan holds until a prospective signed v2 no-start proof or a separately
authenticated retained no-B reconciliation authorizes a distinct fresh frontier.
Completed-cohort retirement is not a recovery path for this failed cohort.

`src/closer_native_readmission.py` provides the explicit recovery command. Its
`plan` and `apply` modes take `--kind prospective`, `--manifest`, `--campaign`,
`--worker`, `--new-manifest`, `--new-worker-config`, `--new-state`, and
`--frontier`. Configuration and frontier arguments are saved JSON files;
`apply` additionally requires the exact `--expected-plan-sha256` returned by
`plan`. The new frontier keeps the original targets and revisions and uses a new
frontier identity. Ordinary recovery keeps action bindings too. A controlled
campaign may bind its action to its newly qualified source installation;
admission independently re-observes that exact action before clearing its hold.
The one-use reservation is keyed to the original
result, so changing the new frontier cannot consume the same proof again.
The original campaign and result remain unchanged. A failed creation after
reservation is an uncertain preparation to reconcile, not a retry instruction.

For a genuinely new finite-v2 no-start producer whose wrapper must change,
the supported sequence is deliberately different: first use
`closer_native_wrapper_install.py --kind prospective plan|apply`, then run the
ordinary model-free qualification for the **new** manifest. Next consume only
the installer's signed current-source known receipt—there are no caller-supplied
worker-config or frontier arguments after CAS:

```sh
/opt/anaconda3/bin/python3 -B src/closer_native_readmission.py prepare-prospective \
  --receipt /absolute/new-observation/wrapper-install-prospective-known.json \
  --expected-plan-sha256 EXACT_PRE_CAS_PLAN_SHA256
```

That command verifies the new-source HMAC seal, intent, and known records plus
the hash-pinned old witnesses, creates only one `source_initialization` HELD
campaign, and reports JSON `held` on any mismatch. It invokes only the new
native contract after CAS. Run the separate `admit` command below with freshly
typed demand to clear that hold; neither wrapper installation nor preparation
admits demand or launches native work. The held campaign's preparation origin
hashes the signed wrapper-known receipt path, raw digest, and pre-CAS plan
digest as provenance. `prepare-prospective` authenticates the HMAC records
once. Later `admit` validates only the derived local immutable preparation
intent/known receipt, current producer, and current manifest; it does not
reopen or reverify the wrapper receipt or old HMAC witnesses.

The supported historical exception is deliberately limited to attempt 13 at
112e9263. `src/closer_retained_reconciliation.py plan|collect --manifest OLD
--campaign OLD_STATE --worker 08854e801de3` validates its exact retained
185-module package, signed admission/handoff/supervisor records, the original
pre-B exception and exact cleanup, current process/lane/claim ownership,
dispatcher sink absence, native index high-water and remote index, and live
984/988/989 association and head. `plan` creates no receipt. `collect` writes
one new signed observation under the original nonce. It never fabricates the
missing old v2 producer proof or rewrites UNKNOWN. The trust model is the
reviewed same-user host documented in `closer_mechanisms.py`.

Historical readmission uses `--kind historical` plus `--reconciliation` naming
a JSON `{path, sha256}` reference to that new observation. It requires the
repaired runtime. Both recovery forms acquire the existing ownership locks,
revalidate before one-use consumption, create only a fresh campaign, and leave
all actual work to ordinary native admission and effect guards. Neither form
permits acceptance, merge, publication, or a model call on its own.

A manifest with `controller_demand` creates the fresh campaign with an atomic
`source_initialization` hold, using its declared identity and controller config.
It cannot use the shared assignment. After native qualification and local
custody validation, admit saved typed demand with:

```sh
/opt/anaconda3/bin/python3 -B src/closer_native_readmission.py admit \
  --manifest /absolute/host-manifest.json --state /absolute/fresh.campaign.json \
  --assignment /absolute/current-assignment.json --request /absolute/current-request.json
```

The finalizer validates current qualification, source projection, custody and
fresh queue observations under ownership locks. `Campaign.begin` clears only
the untouched initial hold in the transaction that stores the admitted snapshot.
Refusal preserves the held campaign. Native entry still owns actual work.

Fresh preparation records an immutable exact-plan `preparation_intent` before
creating its campaign. If storage fails before creation, only the same plan may
retry. If it fails after the untouched initial held record was written, the same
plan may only seal its hash-pinned `preparation_known` receipt; it never calls
`begin` again. Any changed manifest, worker configuration, frontier, state
path, or non-initial worker/effect state remains held.
The first campaign write records the intent reference atomically. Any campaign
with that origin remains unavailable to demand admission or native business
publication until its matching known receipt binds the exact initial state.
