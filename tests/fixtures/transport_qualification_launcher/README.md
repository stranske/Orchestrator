The inert child is a synthetic executable fixture. It never resolves, wraps or
invokes Claude, any model, a network client, or another executable. Tests prepend
the current Python interpreter shebang, copy it to a private temporary path, pin
its exact hash, and inject FixtureRuntime. A fixture name selects a deterministic
failure mode. Child stderr records the received environment/argv/stdin for local
assertions; the runner redacts the synthetic token before retaining it.

The module's default CLI only emits dry-run metadata. --selftest runs eight inert
fixture processes. Neither path calls the native adapter gate. Real execution is
available only through explicit run_wave(execute=True) by the manually authorized
caller after independent review of the exact code/input hashes. Input text and
fixture success do not confer permission, activation or role suitability.

Native integration:

1. Read exact reviewed first-wave-execution-plan.json and first-wave-prompts.json
   bytes; compute ReviewPins only from independently reviewed artifacts. Include
   every declared SOURCE_NAMES hash, not hashes supplied by candidate output.
2. prepare_wave validates profile/model/effort/pool, frozen corpus input hashes,
   exact prompt bytes and fenced argv/config. It is not authorization.
3. Hold held_subscription_lease(wave_id, deadline=<wall clock>) in the calling
   thread. Its production default uses the same HANDOFF_DIR (or real HOME's
   .codex/handoff), closer-profile-execution root, SHA256(claude-subscription)
   lane and bound=1 as closer_dispatcher. offline_root is an explicit test-only
   seam; its minted lease cannot admit NativeRuntime. The lease uses the existing
   finite-frontier slot lifecycle (starting, child and uncertain fences) under an
   external calibration identity. It grants no production frontier, quota read
   or role authority.
4. Within that context call run_wave(..., held_lease=lease, execute=True) with
   an existing OAuth token parameter or existing CLAUDE_CODE_OAUTH_TOKEN. No
   credential file is opened/copied and no login occurs. NativeRuntime rehashes
   the pinned native binary through the existing uncached adapter gate and uses
   adapters.build_command(data_only=True, mode=assess, transport=offload,
   prompt_via_stdin=True) with empty argv prompt. It adds only JSON output and
   max-turns=1. No --verbose, --bare, fallback-model or watchdog is used.
5. Use a private receipt parent. The wave-id directory is created exclusively;
   existence, including after a crash, forbids same-ID replay. Starting and
   started receipts are fsynced before and after Popen; process starts are
   counted once. The caller owns any authorized global accounting integration.

All seven pinned native controls reach the child. Only locale, fixed PATH,
private HOME/cache/config/tmp paths, those controls and the existing OAuth token
enter its environment. Fixed native bounds are <=8 serial submissions, zero
outer retries/fallback, <=45 seconds per process, <=480 seconds per wave,
512 native output tokens, and 65536 combined stdout/stderr bytes per process.
The supervisor kills the process group on timeout/output overflow and cleans up
surviving descendants. Any uncertain/failed process, native envelope, model,
schema, frozen oracle, account restriction or paid-path observation stops the
whole wave. Expected domain UNKNOWN/uncertain answers are valid negative-control
results, distinct from uncertain transport or completion.

The bounds are CLI submissions, not HTTP attempts. Native internal semantic
recovery/renewal may exceed them despite zero retry controls. HTTP attempts,
quota balances and actual billing/subscription charges remain UNKNOWN. Native
cost/token values are estimates. Served effort remains UNKNOWN; imports are
unverified calibration only with missing confinement/provenance evidence and
independent review required. No global registry, ledger or learning record is
written. No provider call is part of this fixture evidence.
