These are inert, synthetic saved-result parser controls. No fixture is a provider
capture, attestation, model evaluation or measured subscription charge.

The positive fixture follows the embedded SDK result and per-model usage schemas
read from installed Claude 2.1.296 (SHA256
`c9b5341637becbd423ddffc5b254afb645682a3868cb708bbc6cc0e7bb419937`).
Read-only inspection established `type=result`, `subtype=success`, `is_error`,
`num_turns`, `result`, `stop_reason`, `terminal_reason`, `session_id`,
`total_cost_usd`, `usage`, and `modelUsage`. Model entries support `inputTokens`,
`outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `costUSD`,
`thinkingTokens`, `webSearchRequests`, `contextWindow`, `maxOutputTokens`,
`canonicalModel`, `provider`, and `costBasis`. `canonicalModel` is pricing lookup
identity; it is not substituted for the raw modelUsage key. No native served
effort or attestation field was observed or invented.

The native schema describes modelUsage as cumulative query-pipeline estimates,
including helpers/compaction and potentially resumed history, rather than billing
statements. Top-level usage can be per-turn or cumulative and is retained separately without
inferring which scope applied to a saved result. A native
success subtype can still report `terminal_reason=turn_setup_failed`; this parser
requires explicit `completed` and `end_turn`, conservatively rejecting other or
missing terminal reasons.

Existing source contracts: `src/adapters.py:_claude_models_in_jsonl` reads CLI
model fields without requested-model fallback; `src/ledger_reconcile.py:_log_cost_usd`
documents Claude result `total_cost_usd`. This importer does not call either
source's filesystem or process paths. It uses `transport_qualification.strict_final`
for duplicate-key, nonfinite, stream and size rejection.

The status fixture models the supplied real zero-model `/usage` observation's
relevant counters: zero turns, zero cost, empty modelUsage. It must fail import as
a model evaluation. All successful imports still require independent review and
carry explicit missing confinement, served effort, quota and attribution evidence.
