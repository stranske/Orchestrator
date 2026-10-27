# Read-only native metadata admission

The production initial-account path is `closer_codex_metadata_admission.metadata_scope(binding, operation)`. It uses the existing real RuntimeBinding private factory origin, qualified physical manifest, existing native `publish_admission` report/signature/frontier, active native entry, actual original queue callback and its physical persisted operation. It never requires or provisions a correction-index namespace, high-water anchor, index writer identity or GitHub effect capability.

The separate source callback is `closer_codex_registration.execute_original`, reached by the existing queue invocation, with exact operation object/frame joins. The original disposition binding contains exactly `runtime_manifest_sha256`, `codex_metadata_qualification_sha256`, and `codex_initial_consumer_sha256`. Its native source report is selected through the existing `CLOSER_CAPACITY_REPORT`; a passed JSON admission is not accepted as a permit. The fixed queue is `<campaign>.codex-metadata-queue.json`; private observation custody is `<campaign>.evidence/codex-account-metadata`, guarded by a distinct nonblocking writer lock. This source-owned housekeeping creates no new signer or access grant.

Inside that scope, `qualify_admitted_callback` retains a create-only intent before either native process. `read_admitted_account` rereads its protected result and returns an opaque one-consumption handle. `retain_admitted_initial_registration` derives the registration solely from authenticated `policy.codex_initial_consumer` (without a supplied account hash), inserts the source-observed digest, and retains a **prospective proposal only**. Installing it is a later source-release decision; the result never authorizes a consumer or task by itself.

The metadata operation is at most35 seconds within native admission, with at most30 seconds shared by its two native observations and cleanup reserve. All earlier unknown intent namespaces remain nonrepeatable. Original controller/index guards remain unchanged. The legacy IndexController-bound entrypoint below is retained for compatibility, but is not required for initial account observation.

# Codex metadata observation

This source operation observes an existing Codex account without starting a thread or task turn. It does not install a consumer, authorize coding, prove confinement, or attest an atomic cloud-policy lease. Native cloud/config/auth loading may update native metadata or caches.

`closer_codex_metadata.qualify_existing_callback(controller, operation)` requires the existing original queue callback and controller's separate held writer lock. It reads the authenticated installation manifest's `policy.codex_metadata_qualification` with exactly these fields:

- `schema`: `closer-codex-account-metadata/v1`
- `binary_path`, `binary_sha256`
- physical `home`, `codex_home`, `private_root`, `cwd`
- `profile_id`

The actual original operation is `disposition`; its `binding.codex_metadata_qualification` equals the controller canonical digest of that selected policy. Selection never comes from a fabricated consumer registration or browser identity.

The operation retains a create-only intent before either native child. An existing intent refuses replay, including after an uncertain outcome. The fixed first child runs the actual `features list` command (which does not support `--strict-config`), with the reviewed fixed first-party/disabled-tool configuration overrides. Its native text renderer is parsed strictly; absent, duplicate, malformed, enabled-plugin or enabled-integration rows hold. The second child uses `app-server --strict-config --stdio` with the same override prefix and closed environment. Its only requests are `initialize`, `initialized`, `account/read` with refresh disabled, and `account/rateLimits/read`. There is no thread, turn, tool, login, reset, configuration write, or retry RPC. Native ordinary-usage boolean presence supports account-match parsing, and its value is not a billing veto.

Both children share at most thirty seconds within the original operation deadline, with two seconds reserved for cleanup and 128 KiB aggregate captured output per child. Raw native diagnostics and account identifiers are not retained in the protected result. Only the account digest, selection/configuration digests and bounded counts remain. Cleanup signals only this still-live owned Popen session, never a recovered PID.

The returned reference is observation data. Initial registration must call `read_protected_account(controller, operation, custody, reference)` within its actual current writer context, then `selected_account_sha256(handle, native_bootstrap=..., profile_id=..., cwd=...)`. This opaque, single-consumption handle rereads the protected intent/result, exact source/configuration and selected route. Its digest is an initial expected identity; the real coding launch still freshly verifies the native account, route, confinement and final admission.

`preflight_owned_route` is wired into the actual owned coding route preparation. It observes features using that route's exact configuration prefix, environment and cwd before appserver Popen, consuming the same original request deadline. A sequential earlier feature observation cannot prove that a remote cloud flag will never change between processes; this limit remains explicit.

The private disposition caller, initial-registration policy assembly and actual root launch recipe are a separately reviewed integration slice. Source tests model protocol, custody and pipe mechanics; they do not establish a live installed account or queue admission.
