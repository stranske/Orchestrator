# Prospective coding-session v2: data preparation only

Historical scope: this document describes the independently reviewed `d569209c` pure-contract stage. Later inactive local supervisor integration is separately unpublished and outside this slice. It does not supply a WORKER caller, authentic enrollment proof, account/confinement qualification, or activation.

This source adds a pure validator, minimum calculation, and immutable policy-snapshot draft helper. It does **not** install a reader, authenticate a session, enroll a policy, change any dispatcher/guard branch, or enable coding. The frozen v1 guards, admission records, public request format, and unsupported native output-token enforcement port remain unchanged.

## Exact private contract

`closer-coding-session-budget/v2` requires `schema`, every limit below, and exactly three knowledge fields: `native_model_attempts`, `native_input_tokens`, and `native_output_tokens`. Each knowledge field must literally be `UNKNOWN`. No token estimate, byte count, one CLI submission, or previous session telemetry establishes native attempts or token usage.

| Required numeric field | Meaning for later source enforcement |
| --- | --- |
| `max_runtime_ms` | Fresh source-owned session duration, further capped by its actual **current** original queue callback deadline. Termination begins at the resulting supervision deadline. |
| `max_cleanup_ms` | Bounded local process-group cleanup grace. Unverified termination persists an uncertainty fence; this is not guaranteed remote cancellation. |
| `max_prompt_utf8_bytes` | Exact final prepared prompt UTF-8 bytes at the canonical offload frame. |
| `max_captured_output_bytes` | Combined raw stdout/stderr bytes retained by the owned pipe collector. |
| `max_retained_final_utf8_bytes` | Post-redaction output content allowed in final output/log persistence and returns. Uncapped raw provider text must not reach an alternate sink. |
| `max_retained_receipt_bytes` | Entire serialized, redacted receipt bytes before atomic persistence or return. |
| `max_outer_submissions` | Exactly `1` outer CLI submission; no outer retry, alternate provider, or fallback. Native internal attempts remain unknown. |

All numbers must be explicit positive integers no larger than `2**53-1`; booleans and floats are rejected. There are no numeric defaults. `minimum_budget(original,current,policy)` validates all three exact schemas and computes the minimum separately for every numeric field. Its result is a data ceiling, not a deadline, authentication result, or permit.

A historical policy's duration does not preserve its old request's temporal authority. An expired original request cannot be reused as a new permission, and no old session may be renewed. A separately reviewed source caller must authenticate any fresh session and its live callback before applying these ceilings. The current source has no such coding-session caller.

## Source policy and new-admission snapshot

The consumer registration's new `coding_session_v2` object has exact schema `closer-coding-session-policy/v2` and fields `consumer_id`, `agent`, `profile_id`, `profile_sha256`, and `budget`. Only the existing Codex `codex-6.1-sol-high` profile is supported; this is a prospective source interface, not installed qualification. Its declared profile digest must later be checked against the actual registered profile and loaded source.

`prepare_admission_policy(consumers)` prepares immutable UTF-8 snapshot bytes only when exactly one source consumer supplies a valid matching policy, runner, and profile binding. Missing, ambiguous, invalid, unsupported, or mixed v1/v2 policy produces a hold reason and no snapshot. The result cannot authenticate the input mapping. The helper accepts no admission object, writes no files, and cannot modify historical records.

Only a later reviewed integration at `IndexController.enroll_source_selection_locked`, while the actual original queue/writer custody and validated SourceInstallation are held, may insert these bytes as the **new** optional `original_coding_session_v2` field of a newly created protected local admission. That snapshot uses exact schema `closer-coding-session-admission/v2`, the policy's identity/budget fields, and its original `policy_sha256`. The containing admission's source authentication supplies authority; a self-hash, copied bytes, model JSON, or caller boolean cannot supply it.

The public original request v1 stays byte-compatible. Already enrolled records reconcile without mutation or backfill. A record with `original_coding_budget` remains subject to v1 semantics; v2 cannot replace or ignore those token caps. Mixed records hold. If no valid owner-selected v2 source policy exists, collection/index enrollment may continue without the new field, and coding stays held. The current real source enrollment does not mint either this field or `original_coding_budget`; tests adding v1 budgets have always been synthetic fixtures.

## Proposed integration, not implemented

1. Review a real source consumer policy with explicit owner-selected numbers and native `UNKNOWN` knowledge. Freeze it only at the new-record construction in real source enrollment; never infer it from the model or supply numeric defaults.
2. Authenticate the exact protected admission and selected consumer. Keep source closure, source body and distinct incumbent context, all findings/tests, native freshness, original minimum limits, actual account/confinement readers, finite pool, writer lock, and durable provider no-repeat intent mandatory. `no_incumbent` remains held pending a separately reviewed native assignment/intake contract.
3. Issue a short-lived, one-use, same-thread/operation/writer-scoped typed context from the reviewed guard lifetime. No serialized permission object or general runner is proposed. Recheck the actual current callback deadline, session provenance, policy, and full source joins at the existing before-Popen seam.
4. Only the selected source-typed coding context may make existing `dispatcher.offload` pass `output_limit` to `run_observed_offload`, cap actual timeout by remaining runtime and live callback time, and require `max_network_retries=0` with no outer fallback. Ordinary `output_limit=None` defaults stay unchanged. Preserve canonical frame/global checks and exact prompt/argv/env/cwd/profile custody. No such context or running change is added by this module.
5. Reuse the bounded stdout/stderr collector and source process-group supervisor with the typed cleanup ceiling. Verify post-redaction final bytes **before every persistence and return**, including failure/partial-output and log paths. Bound the full serialized receipt before `_retain_provider_result` writes. Overflow, timeout, unknown cleanup, or retention failure keeps the provider intent and uncertainty/no-repeat fences; never retry automatically.
6. Record only this session's actual captured/returned facts. Native attempts/tokens remain `UNKNOWN` without fresh qualified observations. Do not read personal past Codex session histories. These output limits do not bound all files written by the coding child or guarantee remote cancellation; enforceable child confinement remains a separate mandatory qualification.

## Real missing WORKER caller

`src/closer_dispatcher.py::Closer.worker` is the nearest existing local worker surface. Its nested `invoke` builds original operation/frontier/deadline callbacks immediately before the existing offload call. However, `action == 'repair'` already returns `closer_mechanical_source.repair_from_source`, and `_execute_profile_route` supports bounded interpretation/classification tasks. The old coding-repair branch is not an available correction-coding owner. No production source invokes `final_provider_guard`.

A reviewed WORKER continuation for the actual admitted, already assigned incumbent subject and original owner is therefore still missing. Do not place a model in Closer ACT, repair the original merged PR, invent an owner, renew an old operation, or treat this pure module as that missing caller. Native account, paid-route, confinement, runtime qualification, and activation also remain held. Source-installation optimization files are outside this change.
