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

The only execution form is deliberately explicit and invokes the manifest's
unchanged canonical native entrypoint, which repeats its own fresh guards:

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

For a finite frontier, an unavailable terminal worker is not retry authority.
The plan holds until a prospective signed v2 no-start proof or a separately
authenticated retained no-B reconciliation authorizes a distinct fresh frontier.
Completed-cohort retirement is not a recovery path for this failed cohort.

`src/closer_native_readmission.py` provides the explicit recovery command. Its
`plan` and `apply` modes take `--kind prospective`, `--manifest`, `--campaign`,
`--worker`, `--new-manifest`, `--new-worker-config`, `--new-state`, and
`--frontier`. Configuration and frontier arguments are saved JSON files;
`apply` additionally requires the exact `--expected-plan-sha256` returned by
`plan`. The new frontier keeps the original targets and action bindings and
uses a new frontier identity. The one-use reservation is keyed to the original
result, so changing the new frontier cannot consume the same proof again.
The original campaign and result remain unchanged. A failed creation after
reservation is an uncertain preparation to reconcile, not a retry instruction.

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
