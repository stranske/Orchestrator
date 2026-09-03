#!/usr/bin/env bash
# orchestrate.sh — one orchestrator tick: capacity -> discover -> plan -> dispatch.
#
# SHADOW (default): runs capacity + discovery + `router --dry-run` and PRINTS the plan it
# WOULD execute. NO claims, NO heartbeat, NO dispatch — so it runs ALONGSIDE the live legacy
# lanes without halting them (the heartbeat is what makes the legacy cron yield).
#
# --active: the real tick — reap stale claims, claim targets, write routing-decision.json +
# the orchestrator heartbeat (legacy cron then yields via handoff-prerun's guard), and
# dispatch agents (each releases its claim on exit). Use --active ONLY after:
#   (1) per-repo worktrees are provisioned (else agents run in $HOME — useless), and
#   (2) `vibe` is installed + logged in (the Mistral lane).
# Until then, run shadow to validate planning against live state with zero disruption.
set -euo pipefail
# Self-locating: code lives in Code/Orchestrator (Dropbox); git checkouts + feedback DB stay LOCAL
# (defaults baked into provision.py/feedback.py). Override with ORCH_DIR.
ORCH_REPO="${ORCH_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# ORCH points at the MODULES, which are not the checkout root any more: a checkout keeps them under
# src/, while the exec mirror is FLAT (orch-sync-mirror.sh copies root-level .py only). Detected,
# never assumed — the same rule paths.py applies in Python, for the same reason: a hardcoded path
# would be right in one tree and wrong in the other, and the mirror is the one launchd runs.
ORCH="$ORCH_REPO/src"
[[ -d "$ORCH" ]] || ORCH="$ORCH_REPO"
# Tools the tick shells out to live outside the default cron/sandbox PATH: ccusage/npx/node
# in homebrew, vibe/cursor-agent in ~/.local|.cursor/bin. Without this, capacity.py can't see
# ccusage → codex/claude read 'unknown' and never get routed.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$HOME/.cursor/bin:$PATH"
# gh auth for the launchd/cron context: gh stores its token in the macOS KEYRING, which a
# launchd-spawned process CANNOT read (it works in an interactive Terminal only). Without this,
# every gh call fails silently -> backlog.py falls back to STALE cache and dispatcher's label
# READ returns empty (rails false-negative -> would re-delegate already-assigned items) while the
# label WRITE fails. The legacy lanes solve it via ~/.codex/bin/with-gh-auth.sh; mirror that here.
# No-op for interactive runs (GH_TOKEN unset, gh uses the keyring fine).
if [[ -z "${GH_TOKEN:-}" && -r "$HOME/.codex/credentials/gh_cli_token" ]]; then
  export GH_CONFIG_DIR="${GH_CONFIG_DIR:-$HOME/.config/gh}"
  export GH_TOKEN="$(<"$HOME/.codex/credentials/gh_cli_token")"
  unset GITHUB_TOKEN
fi
# LangSmith API key (owner-provided in Code/Numbers/values.txt, mirrored to credentials). Needed by
# langsmith_direct.py to pull real agent-automation cost/token telemetry from the LangSmith API —
# the path that actually feeds the Brain's cost learning (the GH-artifact producer chain is starved).
if [[ -z "${LANGSMITH_API_KEY:-}" && -r "$HOME/.codex/credentials/langsmith_api_key" ]]; then
  export LANGSMITH_API_KEY="$(<"$HOME/.codex/credentials/langsmith_api_key")"
  export LANGCHAIN_API_KEY="${LANGCHAIN_API_KEY:-$LANGSMITH_API_KEY}"
fi
# Optional research is opt-in after the 2026-08-29 usage audit found repeated four-judge panels on
# recovered missing-spec inputs. ORCH_RESEARCH_ARM=1 enables new launches and guarded followups;
# missing-spec recovery remains objective-only under every setting. See research_usage_guard.py.
export ORCH_RESEARCH_ARM="${ORCH_RESEARCH_ARM:-0}"
# The tick's own remote dispatch lane (agent:* labels + heartbeat). Default OFF since 2026-09-03:
# 14 dispatches in 30 days, 9 abandoned, none durable, while keepalive ran 1,239 rounds without it.
export ORCH_DISPATCH_LANE="${ORCH_DISPATCH_LANE:-0}"
# GitHub API rate-budget awareness (IMPROVEMENT_BACKLOG.md #8 P2). The local lanes share ONE gh token,
# so gh-heavy cadence steps can hit the REST search (30/min) / core (5000/hr) budget. ORCH_GH_THROTTLE=1
# turns ON the in-loop pace/defer in durability_sweep/keepalive/langsmith (each a no-op when unset).
# _gh_gate skips a step THIS tick only when its resource is SHED, leaving the stamp untouched so the
# next tick retries. Fail-open: gh_capacity --gate exits 75 ONLY on an explicit SHED; any other outcome
# (ok/low/unknown, or even a gh_capacity error) proceeds, so a broken probe never halts the cadence.
export ORCH_GH_THROTTLE="${ORCH_GH_THROTTLE:-1}"
# Pre-delegation adversarial review (2026-07-08, audit item 16d): tick.py already gates HIGH-STAKES
# closer items (explicit high-risk labels only — low volume) through adversarial.py's refute-mode
# minority-veto panel, but the flag was never exported, so the gate reported required_but_not_run
# since it shipped (built != flowing again). Reviewers default to the cheap seats. Advisory:
# adjudicate-don't-obey — vetoes are flags to verify, never an automatic block.
export ORCH_RUN_ADVERSARIAL_REVIEW="${ORCH_RUN_ADVERSARIAL_REVIEW:-1}"
export ORCH_ADVERSARIAL_REVIEWERS="${ORCH_ADVERSARIAL_REVIEWERS:-vibe,gemini}"
# Frontend/runtime-AC checks run from sandboxed automation where direct Chromium launch may be blocked.
# Expect an authorized Chrome/Chromium process to be kept alive on this local CDP endpoint; operators can
# override the port/profile by exporting ORCH_FRONTEND_VERIFY_BROWSER_ENDPOINT before invoking the tick.
export ORCH_FRONTEND_VERIFY_BROWSER_ENDPOINT="${ORCH_FRONTEND_VERIFY_BROWSER_ENDPOINT:-http://127.0.0.1:9222}"
# Optional local GUI keepalive for the endpoint above. Default is off because cron/launchd opening a GUI
# browser is operator policy, but setting ORCH_FRONTEND_VERIFY_START_BROWSER=1 lets the doctor restart a
# local Chrome/Chromium CDP process after reboot/session cleanup before runtime-AC/frontend checks need it.
export ORCH_FRONTEND_VERIFY_START_BROWSER="${ORCH_FRONTEND_VERIFY_START_BROWSER:-0}"
# Stage-2 evidence bridge (RedirectAgent shadow corpus + role:redirect outcome runs). Turned ON per owner
# so linked-disagreement / role-outcome evidence accumulates every tick instead of stalling. SHADOW-ONLY:
# redirect_sweep never kills, releases claims, delegates, or applies redirect_plan — it only runs
# RedirectAgent to PRODUCE advice and records it (caps: max 3 records/tick, 24h dedupe). Backend=cursor
# (cheapest bucket). Export ORCH_REDIRECT_SWEEP_RECORD_CORPUS=0 to pause. (Was unset → sweep ran
# shadow-only and recorded NO role:redirect rows since ~2026-06-25; this restores accumulation.)
export ORCH_REDIRECT_SWEEP_RECORD_CORPUS="${ORCH_REDIRECT_SWEEP_RECORD_CORPUS:-1}"
export ORCH_REDIRECT_SWEEP_BACKEND="${ORCH_REDIRECT_SWEEP_BACKEND:-cursor}"
# Typed role activation (bounded, shadow/advisory): Prompt and Decomposer can author
# dispatch context, Triage compares one bounded backlog snapshot, and Adjudicator runs
# only on genuine persisted-evidence disagreement. Deterministic routing/gates remain
# authoritative; each role is capped once per cycle and all accepted/rejected advice is
# retained as causal feedback evidence. Set ORCH_ROLE_SHADOW=0 for the kill switch.
export ORCH_ROLE_SHADOW="${ORCH_ROLE_SHADOW:-1}"
# Redirect apply bootstrap: ARMED per owner 2026-08-21. The Stage-2 gate
# (ready_for_supervised_apply) is a structural deadlock — synced_role_outcomes counts only APPLIED
# advice, so the gate authorising apply required 10 applied outcomes, and the historical route is
# exhausted (124 replays). Armed, redirect_apply.py applies at most ONE authorised plan per day and
# ONLY on an already-dead lane, so no kill ever runs and the apply reduces to release-claim +
# delegate — what the closer/opener rails already do to a dead stalled lane every hour. It refuses a
# live process, a foreign claim, an un-stamped plan, a repeat target, and it DISABLES ITSELF the
# moment the gate deficits close. Kill switch: ORCH_REDIRECT_APPLY_BOOTSTRAP=0.
# Arming criterion (machine-checkable) lives in capability_recurrence_check.SWITCH_ON_CRITERIA.
export ORCH_REDIRECT_APPLY_BOOTSTRAP="${ORCH_REDIRECT_APPLY_BOOTSTRAP:-1}"
export ORCH_ROLE_MAX_PER_CYCLE="${ORCH_ROLE_MAX_PER_CYCLE:-1}"
# Closer runtime-AC gate (2026-07-08 dormancy disposition: ACTIVATE trial). HARD OPT-IN machine gate —
# fires ONLY on items already carrying a runtime-AC required label OR with a runtime-AC spec file
# (opt-in per issue, low volume). Required active closer/merge work blocks until the gate executes
# and returns PASS. The separate adversarial reviewer panel remains advisory. Shell
# command/non_regression checks stay SEPARATELY gated behind ORCH_RUNTIME_AC_ALLOW_COMMANDS
# (left OFF here) so this never executes an arbitrary agent-authored `command` STRING. Precise scope,
# corrected 2026-08-22: that flag covers the `command` and `non_regression` kinds only (see
# ORCH-ANCHOR: runtime-ac-command-exec-gate). `deliberate_break` runs WITHOUT it — its outer command
# is template-built, and its agent-authored `test_cmd` reaches local_verify via shlex.split with
# shell=False after every shell control character has been rejected. Aligns with the owner
# constraint: prefer machine gates over human review. Export ORCH_RUN_RUNTIME_AC=0 to pause.
export ORCH_RUN_RUNTIME_AC="${ORCH_RUN_RUNTIME_AC:-1}"
# Range-lane LIVE dispatch — BOUNDED TRIAL started 2026-07-08 (owner: "turn it on as a bounded
# trial"). Makes the daily range slot actively dispatch ONE range-lane task/day (testgen/epic/
# codemod/cross_repo/runtime_ac) instead of preview-only, through range_lane_rollout's triple guard.
# REVIEW 2026-07-15 → EXTENDED to 2026-07-22: the first week produced only 2 dispatches (both
# testgen #1279, both transient_infra/rc=137, excluded from quality) and 5 days were dispatch-skipped
# by a stale worktree — thin evidence, below the >=3 quality-bearing bar, so neither keep nor revert
# is earned yet (see 2026-07-15-range-lane-trial-review.md). To revert early: set
# ORCH_RANGE_LANE_ROLLOUT=0 here (or delete this line) and re-sync the mirror.
export ORCH_RANGE_LANE_ROLLOUT="${ORCH_RANGE_LANE_ROLLOUT:-1}"
# SELF-BOUNDING trial (owner constraint: a bounded trial must never silently become permanent, and
# must not depend on an external scheduler firing). Past the review date the flag auto-reverts to
# PREVIEW — the safe default — with a log line, regardless of whether any review session ran. To
# extend or make permanent after review: bump ORCH_RANGE_LANE_TRIAL_UNTIL (and re-sync the mirror).
ORCH_RANGE_LANE_TRIAL_UNTIL="${ORCH_RANGE_LANE_TRIAL_UNTIL:-2026-07-22}"
if [[ "${ORCH_RANGE_LANE_ROLLOUT}" == "1" && "$(date +%Y-%m-%d)" > "$ORCH_RANGE_LANE_TRIAL_UNTIL" ]]; then
  export ORCH_RANGE_LANE_ROLLOUT=0
  echo "  [range-lane] trial window elapsed (until $ORCH_RANGE_LANE_TRIAL_UNTIL) → reverted to PREVIEW (safe default); re-confirm to extend"
fi
_gh_gate() { local rc=0; python3 "$ORCH/gh_capacity.py" --gate "$1" || rc=$?; [[ "$rc" -ne 75 ]]; }
mode="shadow"; [[ "${1:-}" == "--active" ]] && mode="active"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] orchestrate tick: $mode"
STAMP_DIR="${ORCH_STATE_DIR:-$HOME/.codex/orchestrator}"; mkdir -p "$STAMP_DIR" 2>/dev/null || true

# --- Log rotation (every tick, cheap, fail-open) -------------------------------------------------
# This tick's own stdout/stderr go to the handoff cron log via launchd's StandardOutPath, and NOTHING
# rotated it: measured 2026-08-21 at 59.8 MB / 1,876,027 lines, 5x the 11.8 MB the hygiene item
# recorded, still growing. An unrotated log is not just disk -- it is the reason "the GC is dead" was
# concluded from a log whose 102 lines were all the same benign error.
#
# COPYTRUNCATE, deliberately: keep the tail in place and truncate the SAME inode rather than
# renaming. launchd reopens StandardOutPath per invocation so a rename would usually be safe, but a
# rename during a running tick silently detaches the writer and the rest of that tick's output is
# lost to a deleted file. Truncating in place cannot do that.
_rotate_log() {  # $1=path  $2=max bytes (default 16MiB)
  local f="$1" cap="${2:-16777216}" sz
  [[ -f "$f" ]] || return 0
  sz="$(wc -c < "$f" 2>/dev/null || echo 0)"
  [[ "$sz" -gt "$cap" ]] || return 0
  gzip -c "$f" > "${f%.log}-$(date +%Y%m%d%H%M).log.gz" 2>/dev/null || return 0
  tail -n 2000 "$f" > "$f.keep" 2>/dev/null && cat "$f.keep" > "$f" && rm -f "$f.keep"
  # Keep the last 8 archives; unbounded archives are the same defect one directory over.
  ls -1t "${f%.log}"-*.log.gz 2>/dev/null | tail -n +9 | while read -r old; do rm -f "$old"; done
  echo "  rotated $(basename "$f") (was $sz bytes)"
}
_rotate_log "$HOME/.codex/handoff/orchestrator-cron.log" "${ORCH_LOG_MAX_BYTES:-16777216}"
for _lg in "$STAMP_DIR"/*.log; do
  [[ -e "$_lg" ]] || continue
  _rotate_log "$_lg" "${ORCH_LOG_MAX_BYTES:-16777216}"
done
unset _lg
# Cadence identity/stamps/days are generated from one typed registry also read by
# observability_dashboard.py. Refuse to run with an unreadable registry: falling
# back to duplicated shell constants would recreate invisible failure stamps.
if ! cadence_shell="$(python3 "$ORCH/cadence_registry.py" shell)"; then
  echo "  ABORT: cadence registry unavailable" >&2
  exit 1
fi
eval "$cadence_shell"
# PREFLIGHT: --active must never act on stale/blind state. If gh is unauthenticated (keyring
# unreadable under launchd AND no token file), the rails can't read labels (false-negative) and
# writes fail -> abort rather than delegate blind. Shadow may proceed (dry-run, read-only).
if [[ "$mode" == "active" ]] && ! gh auth status >/dev/null 2>&1; then
  echo "  ABORT: gh not authenticated (keyring unreadable under launchd, token file missing/unreadable);" >&2
  echo "         refusing --active so we don't delegate on stale or blind state." >&2
  exit 1
fi

# ORCH-ANCHOR: heartbeat-export ------------------------------------------------------------------
# `capabilities.production_heartbeat` and `daily_heartbeat` are NO-OPS unless
# ORCH_CAPABILITY_HEARTBEATS=1 is in the CHILD process's environment. So this export must sit above
# every heartbeat-emitting producer, or the producer runs and records nothing — and "recorded
# nothing" is indistinguishable from "never ran", which is the silence this whole ledger exists to
# break. Measured 2026-08-22: the export sat BELOW `capacity.py` and the frontend doctor, so
#   * `frontend-verifier` could never accrue a single invocation (the doctor is its only tick
#     caller), making ORCH_FRONTEND_VERIFY_START_BROWSER=1 look like a switch that did not help; and
#   * `windowed-capacity-policy`'s declared cadence "every tick (capacity.build at the top of the
#     tick)" was false — its evidence came only from LATER in-process callers.
# DO NOT cite this block by line number; cite the anchor above. The stored criterion for
# ORCH_FRONTEND_VERIFY_START_BROWSER cited `orchestrate.sh:133`/`:152` and both had rotted to
# 171/190 by the time anyone read them. Enforced by test_heartbeat_ordering.py, which fails if any
# producer that calls production_heartbeat/daily_heartbeat is invoked above this anchor.
#
# Ordering INSIDE this block is also load-bearing and unchanged: validate lifecycle truth FIRST,
# then enable heartbeats. Invalid active declarations are a dispatch blocker — continuing would
# manufacture evidence outside the declared producer/consumer/outcome contract.
if [[ "$mode" == "active" ]]; then
  if ! python3 "$ORCH/capabilities.py" --json validate > "$STAMP_DIR/capability-validation.json"; then
    echo "  ABORT: capability lifecycle validation failed; see $STAMP_DIR/capability-validation.json" >&2
    exit 1
  fi
  if [[ "${ORCH_CAPABILITY_HEARTBEATS:-1}" != "1" ]]; then
    echo "  ABORT: active ticks require ORCH_CAPABILITY_HEARTBEATS=1" >&2
    exit 1
  fi
  export ORCH_CAPABILITY_HEARTBEATS=1
fi

# ORCH-ANCHOR: heartbeat-producers ---------------------------------------------------------------
# Everything from here down may emit capability heartbeats. Capacity + discovery write only
# orchestrator-owned artifacts (capacity.json/backlog.json); the legacy lanes never read them, so
# this is safe in either mode.
python3 "$ORCH/capacity.py"        >/dev/null 2>&1 || echo "  warn: capacity.py failed (continuing)"
python3 "$ORCH/backlog.py" --live  >/dev/null 2>&1 || echo "  warn: backlog.py failed (continuing)"
# ORCH-ANCHOR: frontend-verify-doctor -- the ONLY tick caller of the frontend-verifier capability.
if [[ "${ORCH_FRONTEND_VERIFY_START_BROWSER:-0}" == "1" ]]; then
  if python3 "$ORCH/frontend_verify.py" --doctor --require-browser-endpoint --start-browser >/dev/null 2>&1; then
    :
  else
    echo "  warn: frontend_verify browser endpoint not ready after start attempt (continuing)"
  fi
fi

if [[ "$mode" == "active" ]]; then
  # THE DISPATCH LANE IS SHADOW BY DEFAULT (assessment 2026-09-03, item 1). Claims and the heartbeat
  # exist only to protect this tick's own dispatches; with ORCH_DISPATCH_LANE=0 there are none, so
  # neither runs and the lanes never yield to us. tick.py still runs: it INGESTS keepalive outcomes
  # live (the Brain's evidence) and only PLANS delegation. Announced every tick.
  if [[ "${ORCH_DISPATCH_LANE:-0}" == "1" ]]; then
    python3 "$ORCH/claims.py" reap     >/dev/null 2>&1 || true
    # Heartbeat so the legacy opener/closer cron lanes YIELD to the orchestrator this tick (stale in 15m).
    printf '{"generated_at": %s, "pid": %s}\n' "$(date +%s)" "$$" > "$HOME/.codex/handoff/orchestrator.json"
  else
    echo "  dispatch lane: shadow (ORCH_DISPATCH_LANE=0) — no claims, no heartbeat; tick ingests live, delegates nothing"
  fi
  # REMOTE model (owner's design): for each backlog item, choose a keepalive agent (reserve-aware) ->
  # apply agent:<X> -> the GitHub keepalive runs it on REMOTE capacity -> ingest the PR outcome into the
  # feedback loop. Local CLI delegation (router.py + dispatcher.py) remains available for bounded local work.
  python3 "$ORCH/tick.py" --active   || { echo "  remote tick failed; aborting"; exit 1; }
  # Experiment follow-up (2026-07-08, audit item 12b follow-through): the tick LAUNCHES A/B/C
  # experiments but nothing ever ran collect/evaluate on them — ZERO tick-* evaluation rows existed
  # (all judge evidence came from manual/backfill campaigns), so the research arm burned implementer
  # capacity with no learning signal. Cap 1 experiment/tick; cheap scan when nothing is eligible.
  # Judge scores AND objective anchors both record inside evaluate(). Per-tick retry is intended
  # (no daily stamp): failures here must not go quiet for a day.
  echo "  [cadence] experiment follow-up (collect+evaluate finished A/B/C runs)"
  python3 "$ORCH/exp_abcd.py" followup >/dev/null 2>&1 || echo "  warn: experiment followup failed (continuing)"
else
  # SHADOW: print what the remote tick WOULD delegate; applies NO labels, no heartbeat, no ingest writes.
  python3 "$ORCH/tick.py"
fi

# --- Learning cadence (fail-open; safe in BOTH modes — only updates the feedback store) --------------
# Closes the loop "in production" (IMPROVEMENT_BACKLOG.md #1): DAILY durability sweep resolves
# pending->durable/reverted/reopened so the learner's success label is un-gameable; WEEKLY relearn
# re-estimates the VERSIONED route_weights from accumulated outcomes (the router already reads
# current_weights). Stamp-gated so the hourly tick runs them at the right cadence; never breaks the tick.
_due() { [[ ! -f "$1" ]] && return 0; [[ -n "$(find "$1" -mtime +"$2" 2>/dev/null)" ]]; }   # due if missing or older than $2 days
# --- Per-step kill switch -------------------------------------------------------------------------
# ONE mechanism giving every step a real off-lever, instead of a bespoke ORCH_* flag per capability.
# Added 2026-08-21: the admission gate flagged six capabilities with "nothing can stop it without a
# code change", and six new single-use flags would be six new untested branches on hot paths -- an
# untested kill switch is theatre, because it reports a control nobody has proved works.
#
#   ORCH_DISABLE_STEPS="feature-scan,redirect-sweep"   # comma or space separated
#
# THREE PROPERTIES, each of them the fix for a failure this repo has already paid for:
#  1. IT ANNOUNCES ITSELF EVERY TICK. A silent disable is the latched-gate pattern exactly -- a thing
#     that stays off because nothing says it is off. Every skipped step prints a line, so a disable
#     left in place is visible in the very next log instead of five weeks later.
#  2. IT TOUCHES NO STAMP. Re-enabling makes the step immediately due; the switch defers work, it
#     does not fake completion. (A disable that marked success would silently skip a whole cadence.)
#  3. IT REJECTS UNKNOWN KEYS LOUDLY. `ORCH_DISABLE_STEPS=feature-scanned` would otherwise disable
#     nothing while the operator believes the step is off -- a control that lies is worse than none.
#     Unknown names WARN and are ignored, so a typo can never silently leave a step running.
# Fails toward MOTION: unset, empty or unparseable means nothing is disabled.
_step_disabled() {   # $1=step key -> 0 (true) when the operator has disabled it
  local key="$1" raw entry
  raw="${ORCH_DISABLE_STEPS:-}"
  [[ -z "$raw" ]] && return 1
  for entry in ${raw//,/ }; do
    [[ "$entry" == "$key" ]] || continue
    echo "  [disabled] $key skipped by ORCH_DISABLE_STEPS (no stamp touched; re-enable and it runs next tick)"
    return 0
  done
  return 1
}
_warn_unknown_disable_steps() {   # a typo must not read as a working switch
  local raw entry
  raw="${ORCH_DISABLE_STEPS:-}"
  [[ -z "$raw" ]] && return 0
  for entry in ${raw//,/ }; do
    cadence_known "$entry" 2>/dev/null && continue
    [[ "$entry" == "redirect-sweep" ]] && continue      # named tick step, not in the cadence registry
    echo "  WARN: ORCH_DISABLE_STEPS names unknown step '$entry' -- nothing was disabled by it" >&2
  done
}
_warn_unknown_disable_steps

_cadence_due() { local stamp; stamp="$(cadence_stamp "$1")" || return 2; _step_disabled "$1" && return 1; _due "$STAMP_DIR/$stamp" "$(cadence_days "$1")"; }
# item 10 (2026-07-08 audit): a FAILING daily/weekly step used to retry EVERY hourly tick forever
# (its success stamp never lands, so _due stays true -- observed: 183 hourly langsmith retries
# burning gh budget and burying real warns). Failures now back off on their own stamp: after a
# failed attempt the step waits ORCH_CADENCE_RETRY_HOURS (default 6) before retrying, and
# >= ORCH_CADENCE_ALERT_FAILS (default 3) consecutive failures print an ALERT line so chronic
# breakage is visible instead of buried. gh-budget SKIPs touch NEITHER stamp (capacity != failure).
_attempt_ok() {  # $1=step key -> ok to attempt when no recent failed attempt
  cadence_known "$1" || { echo "  ABORT: unknown cadence step $1" >&2; return 2; }
  local f="$STAMP_DIR/.fail-$1"
  [[ ! -f "$f" ]] && return 0
  [[ -n "$(find "$f" -mmin +"$(( ${ORCH_CADENCE_RETRY_HOURS:-6} * 60 ))" 2>/dev/null)" ]]
}
_mark_ok()   { rm -f "$STAMP_DIR/.fail-$1" 2>/dev/null || true; }
_mark_success() { local stamp; stamp="$(cadence_stamp "$1")" || return 2; touch "$STAMP_DIR/$stamp"; _mark_ok "$1"; }
_mark_fail() {  # $1=step key, $2=optional hint
  local f="$STAMP_DIR/.fail-$1" n=1
  [[ -f "$f" ]] && n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$f"
  echo "  warn: $1 failed (consecutive=$n; retry in ${ORCH_CADENCE_RETRY_HOURS:-6}h${2:+; $2})"
  if (( n >= ${ORCH_CADENCE_ALERT_FAILS:-3} )); then
    echo "  ALERT: cadence step $1 has failed $n consecutive attempts -- needs owner attention"
  fi
}
# Activation is a machine invariant, not a feature-maturity claim. On every active tick, persist
# expiry transitions, validate all declared capabilities, and regenerate the operator inventory
# from the local lifecycle ledger. Invalid active declarations alert but do not hide the rest of
# the learning cadence; the JSON artifact preserves the exact missing lifecycle edge.
if [[ "$mode" == "active" ]]; then
  echo "  [cadence] capability lifecycle sweep + validation"
  capability_validation_tmp="$STAMP_DIR/capability-validation.json.tmp"
  python3 "$ORCH/capabilities.py" sweep >> "$STAMP_DIR/capability-lifecycle.log" 2>&1 || \
    echo "  warn: capability expiry sweep failed (continuing; see $STAMP_DIR/capability-lifecycle.log)"
  capability_validation_rc=0
  python3 "$ORCH/capabilities.py" --json validate > "$capability_validation_tmp" 2>> "$STAMP_DIR/capability-lifecycle.log" || \
    capability_validation_rc=$?
  if [[ -s "$capability_validation_tmp" ]]; then
    mv "$capability_validation_tmp" "$STAMP_DIR/capability-validation.json"
  else
    rm -f "$capability_validation_tmp"
  fi
  if python3 "$ORCH/capabilities.py" inventory > "$STAMP_DIR/capability-inventory.md.tmp" 2>> "$STAMP_DIR/capability-lifecycle.log"; then
    mv "$STAMP_DIR/capability-inventory.md.tmp" "$STAMP_DIR/capability-inventory.md"
  else
    rm -f "$STAMP_DIR/capability-inventory.md.tmp"
    capability_validation_rc=1
  fi
  if (( capability_validation_rc == 0 )); then
    _mark_ok capability-lifecycle
  else
    _mark_fail capability-lifecycle "see $STAMP_DIR/capability-validation.json"
  fi
fi
# Pattern-to-capability compiler cadence: export only accepted, redacted-safe completion episodes
# and mine them into a durable candidate/tombstone state. This is read-only from the Brain's
# perspective; the state/report artifacts are local operator evidence. A failed run backs off and
# remains visible instead of silently starving continuous learning.
# Layer 3 evidence acquisition. SHADOW unless ORCH_EVIDENCE_ACQUISITION=1: it computes which
# evidence-starved capability it would feed and writes the plan, routing nothing. Reports the
# feedable count so "nothing happened" is a stated number rather than an absent line -- the whole
# lesson of the pattern-miner outage. `feedable 0` is expected today: every capability short of its
# threshold is held by a documented default-off switch, which unblock() refuses to feed.
if _cadence_due evidence-acquisition && _attempt_ok evidence-acquisition; then
  echo "  [cadence] evidence acquisition (Layer 3; shadow unless ORCH_EVIDENCE_ACQUISITION=1)"
  if python3 "$ORCH/evidence_acquisition.py" --json \
       --write "$STAMP_DIR/evidence-acquisition-plan.json" \
       > "$STAMP_DIR/evidence-acquisition.log" 2>&1; then
    python3 - "$STAMP_DIR/evidence-acquisition-plan.json" <<'EVIDENCE_ACQ' || true
import json, sys
try:
    p = json.load(open(sys.argv[1])) or {}
except Exception as exc:
    print(f"  EVIDENCE-ACQ: plan unreadable ({exc})"); raise SystemExit(0)
print(f"  EVIDENCE-ACQ: {p.get('state')} — {p.get('summary')}"
      f"{' [LIVE]' if p.get('live') else ' [shadow]'}")
EVIDENCE_ACQ
    _mark_success evidence-acquisition
  else
    _mark_fail evidence-acquisition "see $STAMP_DIR/evidence-acquisition.log"
  fi
fi
if _cadence_due pattern-miner && _attempt_ok pattern-miner; then
  echo "  [cadence] completion-event export + pattern-to-capability mining (daily)"
  # Keep the JSONL suffix on the temporary path so the miner selects its
  # line-oriented parser before the atomically published final artifact.
  pattern_events_tmp="$STAMP_DIR/completion-events.tmp.jsonl"
  pattern_state="$STAMP_DIR/pattern-miner-state.json"
  pattern_status="$STAMP_DIR/pattern-miner-status.json"
  pattern_inventory="$STAMP_DIR/pattern-miner-inventory.json"
  if python3 "$ORCH/feedback.py" completion-events --jsonl --limit "${ORCH_PATTERN_MINER_RUN_LIMIT:-1000}" > "$pattern_events_tmp" && \
     python3 "$ORCH/pattern_miner.py" run --events "$pattern_events_tmp" --state "$pattern_state" \
       --status-out "$pattern_status" --inventory-out "$pattern_inventory" > "$STAMP_DIR/pattern-miner.log" 2>&1; then
    mv "$pattern_events_tmp" "$STAMP_DIR/completion-events.jsonl"
    # LEGIBILITY. Exit code alone said "ran"; it could not say "mined". A run that accepted 0 of
    # 1784 events called _mark_success and printed a checkmark, which is how a dead miner looked
    # healthy for 43 days while ALERTing every 6h into an unread log. The step still succeeds when
    # it RAN -- conflating "the miner is broken" with "the input carries no research subject" is
    # the same mistake in the other direction -- but the line now carries the numbers, so the log
    # is a diagnosis instead of a checkmark. Blocking and drainable quantity in one place.
    python3 - "$pattern_status" <<'MINING_HEALTH' || true
import json, sys
try:
    health = (json.load(open(sys.argv[1])) or {}).get("mining_health") or {}
except Exception as exc:
    print(f"  MINING: status unreadable ({exc})"); raise SystemExit(0)
state = health.get("state", "unknown")
print(f"  MINING: {state} — {health.get('summary', 'no summary')}"
      f" | episodes={health.get('complete_episode_count', '?')}"
      f" candidates={health.get('candidate_count', '?')}")
if health.get("actionable"):
    # Named, not silent: `rejecting` means real defects in the stream, `no_input` means the
    # exporter produced nothing. Neither is drained by waiting.
    print(f"  MINING-ACTIONABLE: {health.get('detail', state)}")
MINING_HEALTH
    _mark_success pattern-miner
  else
    rm -f "$pattern_events_tmp"
    _mark_fail pattern-miner "see $STAMP_DIR/pattern-miner.log"
  fi
fi
# Every tick: classify active local claims and persist redirect/decompose advisories.
# SHADOW-ONLY: redirect_sweep.py never kills, releases claims, delegates, or applies redirect_plan.
if _step_disabled redirect-sweep; then :; else
echo "  [cadence] redirect watch sweep (shadow-only, no live action)"
redirect_sweep_args=(--write "$STAMP_DIR/redirect-sweep.json")
# Optional evidence bridge for IMPROVEMENT_BACKLOG.md #5. When explicitly enabled, the sweep dispatches
# RedirectAgent on capped actionable redirect/decompose reports and appends shadow corpus rows. It still
# never kills, releases claims, delegates replacement work, or applies redirect_plan.
if [[ "${ORCH_REDIRECT_SWEEP_RECORD_CORPUS:-0}" == "1" ]]; then
  redirect_sweep_args+=(--record-corpus --dispatch-redirect-agent)
  [[ -n "${ORCH_REDIRECT_SWEEP_BACKEND:-}" ]] && redirect_sweep_args+=(--backend "$ORCH_REDIRECT_SWEEP_BACKEND")
  [[ -n "${ORCH_REDIRECT_SWEEP_ACTIONS:-}" ]] && redirect_sweep_args+=(--shadow-actions "$ORCH_REDIRECT_SWEEP_ACTIONS")
  [[ -n "${ORCH_REDIRECT_SWEEP_MAX_RECORDS:-}" ]] && redirect_sweep_args+=(--max-shadow-records "$ORCH_REDIRECT_SWEEP_MAX_RECORDS")
  [[ -n "${ORCH_REDIRECT_SWEEP_DEDUPE_HOURS:-}" ]] && redirect_sweep_args+=(--dedupe-hours "$ORCH_REDIRECT_SWEEP_DEDUPE_HOURS")
  [[ -n "${ORCH_REDIRECT_SHADOW_CORPUS:-}" ]] && redirect_sweep_args+=(--corpus "$ORCH_REDIRECT_SHADOW_CORPUS")
fi
if python3 "$ORCH/redirect_sweep.py" "${redirect_sweep_args[@]}" >> "$STAMP_DIR/redirect-sweep.log" 2>&1; then :; else echo "  warn: redirect_sweep failed (continuing; see $STAMP_DIR/redirect-sweep.log)"; fi
fi   # end: _step_disabled redirect-sweep
if _cadence_due keepalive-stage2-plan && _attempt_ok keepalive-stage2-plan; then
  if _gh_gate search && _gh_gate core; then
    echo "  [cadence] keepalive supervisor Stage 2 live plan (daily; read-only surfacing)"
    stage2_plan_json="${ORCH_KEEPALIVE_STAGE2_PLAN_JSON:-$STAMP_DIR/keepalive-supervisor-stage2-plan.json}"
    stage2_plan_tmp="$stage2_plan_json.tmp"
    stage2_report_dir="${ORCH_KEEPALIVE_STAGE2_REPORT_DIR:-$STAMP_DIR/keepalive-supervisor-stage2}"
    stage2_backend="${ORCH_KEEPALIVE_STAGE2_BACKEND:-cursor}"
    mkdir -p "$(dirname "$stage2_plan_json")" "$stage2_report_dir" 2>/dev/null || true
    if python3 "$ORCH/keepalive_supervisor.py" --stage2-plan --stage2-backend "$stage2_backend" --write-report-dir "$stage2_report_dir" --json > "$stage2_plan_tmp"; then
      mv "$stage2_plan_tmp" "$stage2_plan_json"
      _mark_success keepalive-stage2-plan
    else
      rm -f "$stage2_plan_tmp"
      _mark_fail keepalive-stage2-plan
    fi
  else echo "  [cadence] keepalive Stage 2 live plan SKIPPED — gh budget shed (stamp untouched; retry next tick)"; fi
fi
if _cadence_due keepalive-ingest && _attempt_ok keepalive-ingest; then
  if _gh_gate core; then
    echo "  [cadence] keepalive outcome ingest (daily; source=keepalive agent + non-agent PRs into the Brain)"
    if python3 "$ORCH/keepalive_outcomes.py" --lookback-days 7 --include-non-agent >> "$STAMP_DIR/keepalive-ingest.log" 2>&1; then _mark_success keepalive-ingest; else _mark_fail keepalive-ingest "see $STAMP_DIR/keepalive-ingest.log"; fi
  else echo "  [cadence] keepalive ingest SKIPPED — gh core budget shed (stamp untouched; retry next tick)"; fi
fi
if _cadence_due local-outcomes-ingest && _attempt_ok local-outcomes-ingest; then
  if _gh_gate core; then
    echo "  [cadence] local delegate outcome ingest (daily; orchestrator/issue-N PR branches into the Brain)"
    # Log like keepalive-ingest above. This step emits exactly the diagnostics needed to tell a
    # healthy "nothing to ingest" from a stuck join — pending/recorded/skipped plus per-run
    # skipped_details — and used to send all of it to /dev/null, so a run that exited 0 while
    # ingesting nothing was indistinguishable from one that ingested everything (2026-08-09).
    if python3 "$ORCH/outcomes.py" --mode local >> "$STAMP_DIR/local-outcomes-ingest.log" 2>&1; then _mark_success local-outcomes-ingest; else _mark_fail local-outcomes-ingest "see $STAMP_DIR/local-outcomes-ingest.log"; fi
  else echo "  [cadence] local outcomes ingest SKIPPED — gh core budget shed (stamp untouched; retry next tick)"; fi
fi
if _cadence_due capability-outcome-bridge && _attempt_ok capability-outcome-bridge; then
  # Propagate run outcomes into the capability ledger. Without this, capabilities record that they
  # RAN but never how the work turned out, so every gate reads as starved (2026-08-09: only 2 of 33
  # capabilities had any outcome_links while the Brain held 3,825 outcomes). No gh calls — pure
  # local join — so it needs no budget gate. Idempotent: safe to re-run.
  echo "  [cadence] capability outcome bridge (daily; run outcomes -> capability ledger)"
  if python3 "$ORCH/capability_outcome_bridge.py" >> "$STAMP_DIR/capability-outcome-bridge.log" 2>&1; then _mark_success capability-outcome-bridge; else _mark_fail capability-outcome-bridge "see $STAMP_DIR/capability-outcome-bridge.log"; fi
fi
if _cadence_due redirect-apply-link && _attempt_ok redirect-apply-link; then
  # The consumer redirect_plan.apply_plan never had. TWO parts, both local (no gh, no budget gate):
  #   --link-outcomes  ALWAYS ON, mutates nothing: for every redirect role run whose stamped
  #                    dispatch reached a terminal outcome, append the corpus outcome link. This is
  #                    what makes synced_role_outcomes climb without an owner running link-outcome
  #                    by hand (5 links in ~2 months under the manual design).
  #   --apply          self-gated on ORCH_REDIRECT_APPLY_BOOTSTRAP (default 0). With the flag off it
  #                    returns immediately and spends no offload. Armed, it applies at most one
  #                    authorised plan per day on an ALREADY-DEAD lane and disarms itself once the
  #                    Stage-2 deficits close. See SWITCH_ON_CRITERIA in
  #                    capability_recurrence_check.py for the machine-checkable arming condition.
  echo "  [cadence] redirect apply/link (daily; link applied-redirect outcomes, then self-gated apply)"
  redirect_apply_ok=1
  python3 "$ORCH/redirect_apply.py" --link-outcomes >> "$STAMP_DIR/redirect-apply.log" 2>&1 || redirect_apply_ok=0
  python3 "$ORCH/redirect_apply.py" --apply >> "$STAMP_DIR/redirect-apply.log" 2>&1 || redirect_apply_ok=0
  python3 "$ORCH/redirect_apply.py" --status >> "$STAMP_DIR/redirect-apply.log" 2>&1 || true
  if [[ "$redirect_apply_ok" == "1" ]]; then _mark_success redirect-apply-link; else _mark_fail redirect-apply-link "see $STAMP_DIR/redirect-apply.log"; fi
fi
if _cadence_due switch-review && _attempt_ok switch-review; then
  # Held switches must be revisited, not forgotten. ORCH_RANGE_LANE_ROLLOUT was turned on
  # 2026-07-08, reviewed 07-15, extended to 07-22 — and then silently ended up off with no recorded
  # decision. This re-raises (a) a switch held off whose precondition is recorded, and (b) a switch
  # that is ON but whose capability logged no invocation in a week, which is the range-lane failure
  # mode exactly. Questions are non-blocking and auto-ratify to "keep the current position".
  echo "  [cadence] switch review (weekly; held + on-but-idle switches)"
  switch_args=(--json)
  [ "${ORCH_SWITCH_REVIEW:-}" = "1" ] && switch_args+=(--raise)
  if python3 "$ORCH/switch_review.py" "${switch_args[@]}" \
       > "$STAMP_DIR/switch-review.json" 2>> "$STAMP_DIR/switch-review.log"; then
    _mark_success switch-review
  else
    _mark_fail switch-review "see $STAMP_DIR/switch-review.log"
  fi
fi
if _cadence_due coverage-testgen-trigger && _attempt_ok coverage-testgen-trigger; then
  # THE CADENCE ROW IS NOT THE CALLER. `cadence_registry.CADENCE_STEPS` describes a step; this
  # block is what runs it, and registering the row without writing this left the ledger claiming a
  # caller that did not exist -- caught by capability_activation_audit as `entrypoint_no_caller`,
  # which is this repository's founding defect wearing the costume of a completed obligation.
  #
  # Reads each repo's combined coverage and decides whether it buys a testgen invocation: below 90
  # the machine acts, below 85 the owner is told ONCE, on the crossing. Default-OFF behind
  # ORCH_COVERAGE_TESTGEN, so an unset flag decides nothing and says so.
  echo "  [cadence] coverage testgen trigger (weekly; below 90 writes, below 85 warns on a fall)"
  if python3 "$ORCH/coverage_testgen_trigger.py" --json \
       --repo "${ORCH_COVERAGE_REPO:-stranske/Orchestrator}" \
       --coverage-json "${ORCH_COVERAGE_REPORT:-$ORCH_REPO/coverage.json}" \
       --repo-path "${ORCH_COVERAGE_REPO_PATH:-$ORCH_REPO}" \
       > "$STAMP_DIR/coverage-testgen-trigger.json" 2>> "$STAMP_DIR/coverage-testgen-trigger.log"; then
    _mark_success coverage-testgen-trigger
  else
    _mark_fail coverage-testgen-trigger "see $STAMP_DIR/coverage-testgen-trigger.log"
  fi
fi
# Weekly: DOES each capability fire, and did one stop? The can-fire audit and `capabilities usage`
# are both snapshots; nothing stored firing history, so a capability that fired last week and went
# quiet this week looked identical to a healthy one. switch_review covers exactly that silence but
# only for the five gated switches. Read-only apart from its own history file; the regression alarm
# needs at least two snapshots, so the first run only establishes a baseline.
if _cadence_due capability-firing-monitor && _attempt_ok capability-firing-monitor; then
  echo "  [cadence] capability firing monitor (weekly; regressions + overdue against declared cadence)"
  if python3 "$ORCH/capability_firing_monitor.py" --record --json \
       > "$STAMP_DIR/capability-firing-monitor.json" 2>> "$STAMP_DIR/capability-firing-monitor.log"; then
    _mark_success capability-firing-monitor
  else
    _mark_fail capability-firing-monitor "see $STAMP_DIR/capability-firing-monitor.log"
  fi
fi
# Does triggering a capability actually help, and should the front door recommend it more? The
# advisor recorded a `match` per candidate and nothing recorded what happened next, so "recommend the
# useful ones more often" had no signal. Pure read of the ledger.
#
# BOTH QUANTITIES ON EVERY RUN. A propensity report with no resolved outcomes looks identical to one
# built on evidence, which is how the pattern miner reported success for 43 days while accepting
# zero events. So the evidence count is echoed next to the experiment count, and a run with no
# evidence says PRIOR-ONLY rather than passing quietly.
if _cadence_due capability-propensity && _attempt_ok capability-propensity; then
  echo "  [cadence] capability propensity (usefulness-weighted recommendation)"
  if python3 "$ORCH/capability_propensity.py" report --json \
       > "$STAMP_DIR/capability-propensity.json" 2>> "$STAMP_DIR/capability-propensity.log"; then
    _prop_ev=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["capabilities_with_evidence"],d["capability_count"],d["experiment_count"],d["resolved_experiment_count"])' \
                 "$STAMP_DIR/capability-propensity.json" 2>/dev/null || echo "? ? ? ?")
    set -- $_prop_ev
    if [[ "${1:-0}" == "0" ]]; then
      echo "    PRIOR-ONLY: 0 of ${2:-?} capabilities have usefulness evidence; ${3:-?} experiments, ${4:-?} resolved — the loop is not learning yet"
    else
      echo "    evidence: ${1} of ${2} capabilities; ${3} experiments, ${4} resolved"
    fi
    # THE PROVENANCE MIX, never printed apart from the rate it qualifies. "11 of 12 useful" read as
    # a measurement of usefulness for as long as nothing said all 12 were self-assessed by the agent
    # that chose the capability, from one model under near-identical instructions.
    _prov=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("verdict_count",0),d.get("verdicts_outcome_derived",0),d.get("capabilities_with_multiple_judge_arms",0))'               "$STAMP_DIR/capability-propensity.json" 2>/dev/null || echo "? ? ?")
    set -- $_prov
    echo "    verdicts: ${1:-?} (${2:-?} outcome-derived, ${3:-?} capabilities with >1 judge arm) — a self-reported-only mix is an opinion mix, not a measurement"
    # THE THIRD ACTION. Promote and demote cannot say "worth having and BROKEN", so a capability
    # that should be fixed could only be silenced. REPORT-ONLY: nothing is applied and nothing is
    # queued for anyone. Both quantities, so "0 proposals" cannot read as patience: the drainable
    # count (repairs recorded) prints beside the blocking one.
    _rep=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("repair_proposal_count",0),d.get("repair_proposals_worth_having",0),d.get("repairs_recorded",0),d.get("find_count",0))'              "$STAMP_DIR/capability-propensity.json" 2>/dev/null || echo "? ? ? ?")
    set -- $_rep
    if [[ "${1:-0}" != "0" || "${4:-0}" != "0" ]]; then
      echo "    repair proposals: ${1} (${2} worth having and broken), repairs recorded ${3}; defect finds ${4} — report only, see $STAMP_DIR/capability-propensity.json"
    fi
    # DETECTION half of the same loop: which surfaces did a capability's work by hand while never
    # selecting it. REPORT-ONLY here on purpose -- `--apply` exists and is deliberately not passed,
    # the same way feature_scan is wired, because a promotion widens a bound set and widening the
    # narrowing mechanism without a diff anyone saw is how it quietly stops narrowing.
    python3 "$ORCH/capability_propensity.py" detect --json \
      > "$STAMP_DIR/capability-selection-detect.json" \
      2>> "$STAMP_DIR/capability-propensity.log" || true
    _det=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(len(d["promotions"]),len(d["demotions"]))' \
             "$STAMP_DIR/capability-selection-detect.json" 2>/dev/null || echo "? ?")
    set -- $_det
    if [[ "${1:-0}" != "0" || "${2:-0}" != "0" ]]; then
      echo "    selection gaps: ${1} promotion(s), ${2} demotion(s) proposed — see $STAMP_DIR/capability-selection-detect.json"
    fi
    _mark_success capability-propensity
  else
    _mark_fail capability-propensity "see $STAMP_DIR/capability-propensity.log"
  fi
fi
if _cadence_due feature-scan && _attempt_ok feature-scan; then
  # The missing caller for features.py (RULE OF THREE). Its registry held 20 entries, all already
  # `hardened`, so the capability looked idle when it was actually BLIND — nothing ever called it.
  # Measured 2026-08-20: 60 of 74 reusable modules in this tree had never been logged, including
  # four created the previous day. REPORT-ONLY: writing entries needs --apply, deliberately not
  # passed here, because bulk-logging production modules at `ad-hoc` would understate their maturity.
  echo "  [cadence] feature scan (reusable structures the registry has never seen)"
  if python3 "$ORCH/feature_scan.py" --json > "$STAMP_DIR/feature-scan.json" \
       2>> "$STAMP_DIR/feature-scan.log"; then
    _mark_success feature-scan
  else
    _mark_fail feature-scan "see $STAMP_DIR/feature-scan.log"
  fi
fi
if _cadence_due capability-activation-audit && _attempt_ok capability-activation-audit; then
  # Can each capability fire AT ALL? Read-only static+ledger analysis; snapshots the reachable
  # count so progress is measured rather than asserted. No gh calls (label index is cached 7d).
  echo "  [cadence] capability activation audit (daily; can each capability fire?)"
  if python3 "$ORCH/capability_activation_audit.py" --snapshot --json \
       > "$STAMP_DIR/capability-activation.json" 2>> "$STAMP_DIR/capability-activation-audit.log"; then
    _mark_success capability-activation-audit
  else
    _mark_fail capability-activation-audit "see $STAMP_DIR/capability-activation-audit.log"
  fi
fi
# ORCH-ANCHOR: tick-capability-evidence ----------------------------------------------------------
# THE TICK NOW CONSULTS THE FRONT DOOR AND RECORDS WHETHER A CAPABILITY HELPED. 21 of the 43
# capabilities live on this tick and four of them are bound to the `tick` surface in
# `capability_advisor.SURFACE_BINDINGS`, yet nothing here had ever called `advise()` and nothing had
# ever recorded an `invocation`/`outcome` edge against an advisory `match`. That is why the
# capability-propensity step above prints PRIOR-ONLY every run: the loop had a measurement and no
# producer. This is the producer, and it accrues hourly with no further human attention.
#
# PLACED HERE, BELOW `ORCH-ANCHOR: heartbeat-export` AND BELOW ALL FOUR STEPS IT GRADES
# (switch-review, capability-firing-monitor, capability-propensity, capability-activation-audit), so
# a step that ran THIS tick is graded on THIS tick. A producer above the heartbeat export runs and
# records nothing; `capability_activation_audit.heartbeat_env_gate` plus
# `test_capabilities.test_no_tick_producer_runs_above_the_heartbeat_export` fail the suite if this
# ever moves up there.
#
# IT CANNOT MANUFACTURE EVIDENCE, which is the real risk at 24 runs/day x 4 capabilities = 96
# potential data points. Two independent bounds, both in `capability_propensity.tick_evidence`:
# the experiment id is scoped to the UTC day (so the ledger idempotency keys admit at most ONE
# verdict per capability per day however many ticks run), and a verdict additionally requires the
# graded capability's own cadence ARTIFACT to have been regenerated since the last evaluation. Those
# cadences are daily and 6-daily, so the graded ceiling is ~1.3 verdicts/day, not 96.
#
# IT CANNOT STALL THE TICK. Read-only apart from the ledger events it exists to write: no gh, no
# network, no subprocess, no dispatch. `tick_evidence_guarded` arms a SIGALRM budget over the one
# blocking wait in the path (the ledger flock) and turns any exception into a reported field, so the
# subcommand exits 0 on a handled failure and the `if` below covers the rest.
# Kill switch: ORCH_TICK_EVIDENCE_DISABLED=1 (module-side, works from any caller) or
# ORCH_DISABLE_STEPS=tick-capability-evidence (shell-side, the repo's one mechanism). Either alone
# makes the tick behave exactly as it did before this wiring existed.
if _step_disabled tick-capability-evidence; then :; else
  if python3 "$ORCH/capability_propensity.py" tick-evidence \
       --budget-seconds "${ORCH_TICK_EVIDENCE_BUDGET_S:-30}" \
       2>> "$STAMP_DIR/tick-capability-evidence.log"; then :; else
    echo "  warn: tick capability evidence failed (continuing; see $STAMP_DIR/tick-capability-evidence.log)"
  fi
fi   # end: _step_disabled tick-capability-evidence
# ORCH-ANCHOR: tick-phase-consult ----------------------------------------------------------------
# AND THE SAME THING FOR THE TICK'S PHASES. The step above consults the BARE `tick` surface, which
# binds four capabilities and grades them. Fourteen more are bound to the tick's PHASES
# (`tick:capacity`, `tick:dispatch`, `tick:experiments`, `tick:redirect`, `tick:learning` -- the
# names come from this script's own first line, "capacity -> discover -> plan -> dispatch", and from
# its `--- Learning cadence ---` / `[cadence] redirect ...` / `[cadence] experiment follow-up`
# blocks). The tick is sub-surfaced because 18 capabilities in ONE reasoning context is the measured
# 13.62% selection condition; each phase resolves to 6-8, inside the 10-20 safe zone.
#
# EXTENDS THE #37 PATTERN, adds no second mechanism: same `capability_advisor.advise()` call, same
# `match` heartbeat, same placement rule (BELOW `ORCH-ANCHOR: heartbeat-export`, so the heartbeat it
# writes is actually enabled -- `test_capabilities.test_no_tick_producer_runs_above_the_heartbeat_export`
# fails the suite if this moves up), same fail-open discipline.
#
# IT DOES NOT MULTIPLY VERDICTS. `capability_propensity.tick_evidence` is the only writer of
# usefulness verdicts and it reads `binding_for("tick")`, which sub-surfacing does not touch: its
# ceiling stays ~1.3/day. This step writes ONLY advisory `match` events, and `_record_matches` keys
# idempotency on a digest of the consult text, which is stable per (surface, UTC day) -- so the
# whole added volume is 34 events on the first tick of each day and zero on the other 23.
#
# IT CANNOT STALL THE TICK. Read-only apart from those events: no gh, no network, no subprocess, no
# dispatch. `consult_phases_guarded` arms a SIGALRM budget over the one blocking wait (the ledger
# flock), fails open PER PHASE so one broken phase cannot silence the others, and the CLI always
# exits 0. Kill switch: ORCH_DISABLE_STEPS=tick-phase-consult (registered in cadence_registry.py,
# so the switch is real rather than a no-op that WARNs).
if _step_disabled tick-phase-consult; then :; else
  if python3 "$ORCH/capability_advisor.py" --consult-tick-phases \
       2>> "$STAMP_DIR/tick-phase-consult.log"; then :; else
    echo "  warn: tick phase consult failed (continuing; see $STAMP_DIR/tick-phase-consult.log)"
  fi
fi   # end: _step_disabled tick-phase-consult
if _cadence_due issue-readiness && _attempt_ok issue-readiness; then
  # Decide which open issues the fleet may work, WITHOUT routing that decision through the owner.
  # `backlog._is_ready` reads a label only a human ever applied, so the ready queue tracked one
  # person's spare time: 94 issues open, backlog at 1. This applies `status: ready` to actionable
  # non-risk issues and sends only risk-labelled ones to a non-blocking, auto-expiring question
  # (measured 2026-08-11: 32 auto / 5 owner -> 6.6 min/week against a 30 min/week budget).
  # Writes are gated on ORCH_ISSUE_AUTOREADY=1; without it this is a read-only assessment.
  if _gh_gate search && _gh_gate core; then
    echo "  [cadence] issue readiness (daily; auto-ready actionable non-risk issues)"
    readiness_json="$STAMP_DIR/issue-readiness.json"
    readiness_tmp="$readiness_json.tmp"
    readiness_args=(--json)
    [ "${ORCH_ISSUE_AUTOREADY:-}" = "1" ] && readiness_args+=(--apply)
    # Repair task-type labels first so classify() can route campaign work to the codemod lane
    # instead of implement. Measured 2026-08-19: the Trend_Model_Project legacy-removal issues
    # (#5852..#5858) carried ZERO labels, so 384 files of mechanical removal routed as `implement`.
    # Only fires where no routing signal already exists; never overrides one.
    if [ "${ORCH_ISSUE_AUTOREADY:-}" = "1" ]; then
      python3 "$ORCH/issue_readiness.py" --apply-task-labels --json \
        >> "$STAMP_DIR/issue-readiness.log" 2>&1 || true
    fi
    if python3 "$ORCH/issue_readiness.py" "${readiness_args[@]}" > "$readiness_tmp" \
         2>> "$STAMP_DIR/issue-readiness.log"; then
      mv "$readiness_tmp" "$readiness_json"
      _mark_success issue-readiness
    else
      rm -f "$readiness_tmp"
      _mark_fail issue-readiness "see $STAMP_DIR/issue-readiness.log"
    fi
  else echo "  [cadence] issue readiness SKIPPED — gh budget shed (stamp untouched; retry next tick)"; fi
fi
if _cadence_due durability-sweep && _attempt_ok durability-sweep; then
  if _gh_gate search; then
    echo "  [cadence] durability sweep (daily)"
    if python3 "$ORCH/durability_sweep.py" >/dev/null 2>&1; then _mark_success durability-sweep; else _mark_fail durability-sweep; fi
  else echo "  [cadence] durability sweep SKIPPED — gh search budget shed (stamp untouched; retry next tick)"; fi
fi
# Refresh promotion readiness and active priors only after late outcomes and
# durability have landed. This consumes exact capability-version joins and is
# idempotent; target-specific rollback execution remains outside this step.
if _cadence_due capability-causal-reconcile && _attempt_ok capability-causal-reconcile; then
  echo "  [cadence] capability causal lifecycle reconciliation (daily)"
  if python3 "$ORCH/capability_lifecycle.py" reconcile-all >> "$STAMP_DIR/capability-lifecycle.log" 2>&1; then
    _mark_success capability-causal-reconcile
  else
    _mark_fail capability-causal-reconcile "see $STAMP_DIR/capability-lifecycle.log"
  fi
fi
# RETIRED 2026-07-08 (audit Gap C close-out): the daily LangSmith GH-ARTIFACT fetch+ingest step
# was removed. It failed on every run for months (the consumer-CI producer chain never writes the
# ndjson artifacts — "starved") and contributed ZERO cost rows, while three OTHER paths now feed
# the cost plane fully: langsmith_direct (API, below — the live source of all source='langsmith'
# rows), ccusage per-run attribution, and ledger_reconcile's native per-run cost/latency harvest
# (item 16j). Duplicative + always-failing → retired rather than fixed. The langsmith_fetch.py
# MODULE is kept: periodic_report.py still calls its artifact-HEALTH diagnostic (read-only), which
# is the honest place for "the producer chain is starved" to surface. Revive this step only if a
# consumer-CI artifact producer is ever actually landed.
# Direct LangSmith API pull — the path that ACTUALLY delivers cost/token data into the Brain. The
# GH-artifact chain above is starved (no consumer CI writes the ndjson; consumer runtime records carry
# no cost + no orchestrator-joinable ref). langsmith_direct shapes workflows-agents runs into
# langsmith-fleet/v1 and reuses langsmith_pull's github_pr/run_id join. Idempotent (costs PK=run_id);
# hits the LangSmith API (not gh) so no gh gate. Verified live 2026-06-19: 95 cost + 193 trace rows.
if _cadence_due langsmith-direct && _attempt_ok langsmith-direct; then
  if [[ -n "${LANGSMITH_API_KEY:-}" ]]; then
    echo "  [cadence] LangSmith direct cost/trace pull (daily; source=langsmith into the Brain)"
    if python3 "$ORCH/langsmith_direct.py" --ingest >/dev/null 2>&1; then _mark_success langsmith-direct; else _mark_fail langsmith-direct; fi
  else echo "  [cadence] LangSmith direct pull SKIPPED — no LANGSMITH_API_KEY"; fi
fi
if _cadence_due ledger-reconcile && _attempt_ok ledger-reconcile; then
  echo "  [cadence] local ledger reconciliation (daily)"
  if python3 "$ORCH/ledger_reconcile.py" reconcile --strict >/dev/null 2>&1; then _mark_success ledger-reconcile; else _mark_fail ledger-reconcile; fi
fi
if _cadence_due ccusage-reconcile && _attempt_ok ccusage-reconcile; then
  echo "  [cadence] ccusage per-run attribution (daily)"
  if python3 "$ORCH/ccusage_reconcile.py" reconcile --strict >/dev/null 2>&1; then _mark_success ccusage-reconcile; else _mark_fail ccusage-reconcile; fi
fi
if _cadence_due range-rollout && _attempt_ok range-rollout; then
  # item 14 (2026-07-08 audit): the five range lanes (testgen/epic/codemod/cross_repo/runtime_ac)
  # were built + selftested but NOTHING invoked them (audit F9: range built != flowing). Daily
  # slot: PREVIEW by default -- plans against the cached backlog, exercising the full
  # filter->router->dispatch-preview path, artifact at $STAMP_DIR/range-rollout.json. Flipping
  # ORCH_RANGE_LANE_ROLLOUT=1 makes this same step actively dispatch (max 1/day) through the
  # module's own triple guard; active ticks only, so shadow runs stay read-only.
  echo "  [cadence] range-lane rollout (daily; preview unless ORCH_RANGE_LANE_ROLLOUT=1)"
  range_args=(--cached-backlog --json --max-dispatches 1)
  if [[ "$mode" == "active" && "${ORCH_RANGE_LANE_ROLLOUT:-0}" == "1" ]]; then
    range_args+=(--apply --confirm-rollout)
  fi
  if python3 "$ORCH/range_lane_rollout.py" "${range_args[@]}" > "$STAMP_DIR/range-rollout.json" 2>>"$STAMP_DIR/range-rollout.log"; then
    _mark_success range-rollout
  else
    _mark_fail range-rollout "see $STAMP_DIR/range-rollout.log"
  fi
fi
if _cadence_due runtime-ac-flow && _attempt_ok runtime-ac-flow; then
  # Live firing and the denominator come from structured runtime_ac_gate events.
  # Legacy cron text is archival only and cannot attach a target to a spec path.
  echo "  [cadence] runtime-AC structured gate-flow monitor (daily; read-only)"
  if python3 "$ORCH/runtime_ac_flow_monitor.py" --json \
      --write-report "$STAMP_DIR/runtime-ac-flow-monitor.json" \
      > "$STAMP_DIR/runtime-ac-flow-monitor.log" 2>&1; then
    _mark_success runtime-ac-flow
  else
    _mark_fail runtime-ac-flow "see $STAMP_DIR/runtime-ac-flow-monitor.log"
  fi
fi
if _cadence_due research-usage-guard && _attempt_ok research-usage-guard; then
  echo "  [cadence] research usage guard report (daily; local/no-LLM)"
  if python3 "$ORCH/research_usage_guard.py" report --fail-on-alert \
      --write-report "${ORCH_STATE_DIR:-$HOME/.codex/orchestrator}/research-usage-report.json" \
      > "$STAMP_DIR/research-usage-report.log" 2>&1; then
    _mark_success research-usage-guard
  else
    _mark_fail research-usage-guard "see $STAMP_DIR/research-usage-report.log"
  fi
fi
if _cadence_due relearn && _attempt_ok relearn; then
  echo "  [cadence] relearn + beliefs report (weekly)"
  if python3 "$ORCH/relearn_report.py" >/dev/null 2>&1; then _mark_success relearn; else _mark_fail relearn; fi
fi
if _cadence_due route-weights-export && _attempt_ok route-weights-export; then
  # Shadow-only local export. Publication is intentionally absent from the hourly path: it needs
  # BOTH --publish and ORCH_ROUTE_WEIGHTS_PUBLISH=1, so a cadence run can never change remote policy.
  echo "  [cadence] route weights export (daily; shadow local artifact, remote publish blocked)"
  if python3 "$ORCH/route_weights_export.py" \
       > "$STAMP_DIR/route-weights-export.log" 2>&1; then
    _mark_success route-weights-export
  else
    _mark_fail route-weights-export "see $STAMP_DIR/route-weights-export.log"
  fi
fi
if _cadence_due periodic-report && _attempt_ok periodic-report; then
  echo "  [cadence] periodic dataset report + observability dashboard (weekly)"
  if python3 "$ORCH/periodic_report.py" --json > "$STAMP_DIR/periodic-report.json" && \
     python3 "$ORCH/observability_dashboard.py" --write-markdown "$STAMP_DIR/observability-dashboard.md" --json > "$STAMP_DIR/observability-dashboard.json"; then
    _mark_success periodic-report
  else _mark_fail periodic-report; fi
fi
# Stage 2 of the DEFERRED keepalive-supervisor path (IMPROVEMENT_BACKLOG.md "Future development"):
# for each OPEN keepalive PR, record keepalive's blunt action vs redirect_policy's shadow
# recommendation vs the eventual outcome -> builds the A/B corpus that would EARN a live supervisor.
# SHADOW-ONLY: keepalive_shadow.py takes NO action on any PR (no second controller -> no split-brain).
# Safe in both modes (records only to the LOCAL corpus). Daily, fail-open, capped.
if _cadence_due keepalive-shadow && _attempt_ok keepalive-shadow; then
  if _gh_gate search; then
    echo "  [cadence] keepalive shadow corpus (daily; shadow-only, no live action)"
    if prs="$(gh search prs --owner stranske --label "agents:keepalive" --state open \
                --json repository,number --jq '.[] | "\(.repository.nameWithOwner)#\(.number)"' 2>/dev/null)"; then
      n=0
      while IFS= read -r pr; do
        [[ -z "$pr" ]] && continue
        python3 "$ORCH/keepalive_shadow.py" --shadow "$pr" >/dev/null 2>&1 || echo "    warn: shadow $pr failed (continuing)"
        n=$((n+1)); [[ "$n" -ge 25 ]] && break
      done <<< "$prs"
      echo "    shadowed $n keepalive PR(s)"
      _mark_success keepalive-shadow
    else
      _mark_fail keepalive-shadow "gh PR search failed"
    fi
  else echo "  [cadence] keepalive shadow SKIPPED — gh search budget shed (stamp untouched; retry next tick)"; fi
fi
# Weekly: backfill the corpus with RESOLVED closed keepalive PRs (7-14 days old -> past the
# durability grace, so merged ones resolve to durable/reverted, not 'merged_pending'). Idempotent
# (dedups already-seeded targets), durability-labeled, fail-open. Complements the daily open-PR
# shadow above so the corpus captures terminal outcomes the open-PR pass never sees.
if _cadence_due keepalive-backfill && _attempt_ok keepalive-backfill; then
  if _gh_gate search; then
    echo "  [cadence] keepalive shadow backfill (weekly; resolved closed PRs + durability)"
    if python3 "$ORCH/keepalive_shadow.py" --backfill --days 14 --limit 40 >/dev/null 2>&1; then _mark_success keepalive-backfill; else _mark_fail keepalive-backfill; fi
  else echo "  [cadence] keepalive backfill SKIPPED — gh search budget shed (stamp untouched; retry next tick)"; fi
fi

# Daily: download and validate new shadow consumer-sync evidence artifacts and compute drift
if [[ "${mode}" == "active" ]] && _cadence_due consumer-sync-artifact-ingest && _attempt_ok consumer-sync-artifact-ingest; then
  if _gh_gate core; then
    consumer_sync_expiry="${ORCH_CONSUMER_SYNC_EVIDENCE_UNTIL:-2026-07-25}"
    consumer_sync_mode="human-on-exception"
    consumer_sync_phase="importer-20260718-20260725"
    consumer_sync_phase_args=(--mode "$consumer_sync_mode" --expiry "$consumer_sync_expiry" --phase-id "$consumer_sync_phase")
    if [[ "$(date +%Y-%m-%d)" > "$consumer_sync_expiry" ]]; then
      consumer_sync_mode="shadow"
      consumer_sync_phase="importer-shadow-after-20260725"
      consumer_sync_phase_args=(--mode "$consumer_sync_mode" --phase-id "$consumer_sync_phase")
    fi
    echo "  [cadence] consumer-sync artifact ingestion bridge (daily; $consumer_sync_mode)"
    if python3 "$ORCH/consumer_sync_artifact_ingest.py" ingest \
         --state-dir "$STAMP_DIR" \
         --max-artifacts 1 \
         --max-repositories 5 \
         "${consumer_sync_phase_args[@]}" \
         --repository "stranske/Template" \
         --repository "stranske/Ready" \
         --repository "stranske/Collab-Admin" \
         --repository "stranske/learning-management-system" \
         --repository "stranske/Fine-Art-Archive" >> "$STAMP_DIR/consumer-sync-artifact-ingest.log" 2>&1; then
      _mark_success consumer-sync-artifact-ingest
    else
      _mark_fail consumer-sync-artifact-ingest "see $STAMP_DIR/consumer-sync-artifact-ingest.log"
    fi
  else echo "  [cadence] consumer-sync artifact ingestion SKIPPED — gh core budget shed (stamp untouched; retry next tick)"; fi
fi
