# Data-only execution parity contract

`closer_data_only_execution.py` is a pure settings/validation contract. It reads
no files, environment, credentials or registry and launches nothing. Its fixed
Claude retry/fallback controls and exact native JSON/max-turns argv come from reviewed
launcher `64a2b140`. This is implemented source, not an installed/deployed claim.

The API is `isolated_environment(private_root, cwd=..., existing_oauth_token=..., limits=...)`,
`expected_argv(binary_path, profile, limits=...)` and `validate_launch(...)`. Inputs must be
explicit. Validation binds actual supplied profile/model/effort, reviewed binary
SHA, three source pins, exact argv, isolated cwd/environment, all control presence,
and the declared response protocol. The returned versioned receipt contains
separate argv/environment/config/binding hashes. OAuth is redacted and its value
is never retained or hashed; the environment hash explicitly describes that scope.
No qualification, served identity, entitlement or runtime attestation is inferred.
Unregistered profile claims must still be refused by the existing registry/role
qualification consumer before calling this helper.

Limits have no production defaults. The exact `closer-data-only-limits/v1`
descriptor requires role, externally verified model-contract SHA, whole-prompt
byte cap, final-result and native-envelope byte caps, output-token cap, max-turns
and timeout. The output-token environment control derives from that descriptor;
the receipt binds its complete content/hash and actual prompt bytes/hash.
Screening's 512-token cap is not a production default. A complete classifier
response with inventory/evidence exceeds 512 bytes; no byte-to-token conversion
is inferred. Final-byte and output-token limits are separate controls. Test limits
are synthetic controls and do not qualify production values. Root must separately
approve those values against the real whole-prompt/response contract, and role
qualification must bind the same descriptor as execution. The existing model
contract descriptor supplies grammar, not a default output cap.

The small synthetic test regime is explicit: 65536 prompt bytes, 8192 final
bytes, 65536 native-result bytes, 2048 output tokens, one turn, and 45 seconds.
These values exercise the helper; they are not approved universal production
limits for either role, and the helper fills none of them. Production limits
must fit complete role responses or route out-of-regime inputs deterministically
before dispatch while preserving every proof and criterion. Their applicability
must be checked against the complete production fixture coverage and bound by
matching qualification. Numeric validation alone does not enable a role or
attest installed source. The actual supervisor timeout is an additional required
launch input: it can narrow to remaining request time but cannot exceed the
descriptor. That actual value is included in the config hash.

`validate_payload(text, limits=..., kind=...)` requires an explicit prompt, final,
or native-result kind and enforces its full UTF-8 byte limit. It neither parses
nor guesses format. The native-envelope 64KiB ceiling preserves the existing
collector input bound; it is not classifier semantics or proof that a token cap
can fit an output. Domain ceilings do not attest provider capability or grant
launch authority. Misplaced OAuth values in prompt/profile/paths are rejected
before any binding hash; only the transient child environment contains the value.

## Required production seams (not wired by this slice)

At base `ccc6b951`, `dispatcher.offload` builds argv around line 3123, composes a
login shell and auth/network preludes around line 3204, and invokes that shell
around line 3633 without an explicit isolated environment. Its data-only stdout
is currently production deliverable text, returned around line 4003. The Claude
adapter already emits the no-tools/MCP/hooks/settings fence, but does not request
native JSON output/max-turns. Their integration requires a single coordinated writer.

The dispatcher/adapter owners must integrate the helper before a production parity
claim: create and physically validate empty private 0700 home/config/cache/tmp/cwd
directories; obtain only the already authorized existing OAuth context; rehash
the actual installed binary uncached immediately before launch; and validate the
actual argv/env/cwd/source pins passed to the existing supervised shell-free
data-only spawn. Remove bash/prelude environment inheritance only for that lane.
Reuse process-broker/supervision APIs rather than adding an untracked Popen path.
Keep original shared
pool leases, durable starting/unknown/group-absence fences, cancellation, deadline
and receipt/accounting handling; pass the same validated timeout to that supervisor.
The pure path checks are lexical and cannot prove symlink
absence, empty directories, ownership or physical confinement.

`claude-native-json-result/v1` is the only admitted protocol. Validation rejects
`production-deliverable-text/v1` and never examines stdout to guess its format.
Switching production output to native JSON requires an explicit collector and
unwrapping step: validate native completion/identity/usage, retain its raw protocol,
then send only its final assistant result to existing Closer response parsing.
Neither arbitrary JSON inside a text deliverable nor its requested model/effort
can mint native completion, served identity or usage evidence. Collector identity
and usage remain imported claims unless the existing trusted runtime binds them.

Stage: helper implemented/tested offline; dispatcher/adapter wiring, integrated
tests, review and deployment remain pending. Candidate trials are screening
evidence and do not qualify this production source version automatically.
