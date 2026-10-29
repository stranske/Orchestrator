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
The plan holds and points to the existing completed-discovery controller-demand
retirement/re-admission path, which requires a distinct fresh frontier.
