# Historical material through the closer

Historical work is input to the current closer. Its requirements, source changes,
CI artifacts, delivery links and retained run records are evidence to evaluate.
A current merged PR does not establish what an earlier uncertain attempt did.

The evaluation-only entry point reuses the production PREPARE and INTERPRET
stages. It never invokes ACT, exception repair, reconciliation or queue execution.
An evaluation receipt has no mutation or acceptance authority. It is not a
production queue completion, an operator attestation or a historical success.

## Representative application

Start with a small immutable batch. Each item contains:

- The real target and current revision, original task body and acceptance criteria.
- Hash-bound current source/delivery observations and retained raw evidence.
- Original run/action records with their actual provenance and missing fields.
- Explicit unknowns, prior-effect risks and the precise permitted evaluation scope.

Use the existing closer to collect evidence, evaluate it and record a disposition.
An item-scoped pause remains a pause. Missing evidence remains missing evidence.
Provider admission, quotas, shared capacity pools, request ownership and
cancellation remain effective. A blocked model invocation is a recorded pending
stage, not permission to choose another route or fabricate a result.

The original state is a pinned input. New packets and evaluation receipts go to
fresh output storage. A retained start or uncertain outcome is not automatically
resumed or replayed. Changing the input revision requires a new explicit batch;
ordinary source drift cannot silently refresh the admitted item.

## Interpreting outcomes

Distinguish completed source work from unfinished delivery. An evaluation may
identify a remaining task without authorizing its execution. Before a prospective
action, the normal closer still needs fresh current revision and action bindings,
duplicate-action checks, ownership and resource admission. Historical UNKNOWNs
remain intact unless a separate supported reconciliation has sufficient evidence
or the operator makes an appropriately specific decision.

For example, a queued exception with no terminal result differs from a completed
source change with a paused delivery. A failed interpretation that retains older
READ and ACT uncertainty cannot be resolved by showing only that the source is
currently merged. Preserve those distinctions in the input and let the closer
state its concrete next step.

Inspect stage receipts for a representative batch before expanding. A read-only
or action-gated evaluation exercises ingestion and decision stages; it does not
establish guarded live mutation acceptance or served-model identity.

## Entry point

```sh
python3 src/closer_dispatcher.py run \
  --evaluate-only \
  --state /absolute/path/to/original-dispatch-state.json \
  --assignment /absolute/path/to/evaluation-assignment.json \
  --evaluation-batch /absolute/path/to/batch.json \
  --evaluation-batch-sha256 <exact-batch-sha256> \
  --evaluation-output /absolute/path/to/fresh-evaluation-directory \
  --runtime-manifest /absolute/path/to/qualified-runtime-manifest.json \
  --request-timeout-seconds 120
```

Do not combine this with `--apply`, `--workers`, `--pipeline`, `--shadow`, effect
dispositions, or a production coordinator/finite-frontier argument. An existing
output directory refuses automatic replay, including after an interrupted or
uncertain provider request. This is a per-invocation boundary, not a batch-global
claim: the CLI does not schedule or automatically retry evaluations. A caller
must not evade an uncertain result by choosing another output directory; a
second assessment needs a separate explicit request or supported reconciliation.
The assignment and batch must name the same ordered
set of at most eight real items. Every current observation, original source,
retained history row and available evidence reference must retain its exact hash.

The input schema is `closer-historical-evaluation-batch/v1`; output is
`closer-historical-evaluation-result/v1`. Each item carries `target`,
`current_revision`, `original_task`, `observation_ref`,
`historical_material_ref`, `available_evidence`, `explicit_unknowns`,
`prior_effect_risks` and `exact_action_scope`. `canonical_history_unchanged`
pins the original raw and pipeline documents. The referenced historical material
uses exact source references and row locators, rather than searching for a hash
anywhere in an unrelated document.

A returned advisory assessment is distinct from `held`, `pending` or `deferred`.
Inspect the retained stage receipts to learn whether PREPARE, deterministic
INTERPRET or a real provider assessment ran. No outcome here releases an old fence
or permits a production action.
