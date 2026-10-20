#!/usr/bin/env python3
"""Fail-closed preview renderer for HANDOFF_EXACT_TOKEN_V1 lane leases.

Reads explicit canonical handoff-prerun.sh / handoff-postrun.sh inputs and writes
patched copies to --output-dir. Does not install or mutate live ~/.codex paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "src" if (ROOT / "src").is_dir() else ROOT
LEASE_MODULE = MODULE_DIR / "closer_native_lease.py"

PRERUN_MUTEX_START = "# --- Per-lane round mutex"
PRERUN_MUTEX_END = "# Capture both stdout and stderr; route by exit code below."

POSTRUN_TRAP_ANCHORS = (
    "trap 'rm -rf \"$LANE_MUTEX_DIR\" 2>/dev/null || true; launch_keepalive_label_repair' EXIT",
    "trap 'rm -rf \"$LANE_MUTEX_DIR\" 2>/dev/null || true' EXIT",
)

MARKER_START = "# BEGIN HANDOFF_NATIVE_LEASE v1 (generated preview — do not edit by hand)"
MARKER_END = "# END HANDOFF_NATIVE_LEASE v1"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fail(msg: str) -> None:
    raise ValueError(msg)


def lease_shell_block() -> str:
    return f"""{MARKER_START}
# Cooperative lease via closer_native_lease.py (HANDOFF_EXACT_TOKEN_V1). No stale reap; no rm -rf.
: "${{CLOSER_NATIVE_LEASE_PY:?CLOSER_NATIVE_LEASE_PY must point at closer_native_lease.py}}"
CLOSER_NATIVE_LEASE_INTERPRETER="${{CLOSER_NATIVE_LEASE_INTERPRETER:-python3}}"
HANDOFF_HANDOFF_ROOT="${{HANDOFF_HANDOFF_ROOT:-$HOME/.codex/handoff}}"
LANE_MUTEX_DIR="${{HANDOFF_LANE_MUTEX_DIR:-$HANDOFF_HANDOFF_ROOT/.lane-$lane.lock}}"
HANDOFF_LANE_TOKEN=""
HANDOFF_EXPECTED_LANE_TOKEN=""
_read_lane_meta_credential() {{
  local _mf="$LANE_MUTEX_DIR/meta"
  [[ -f "$_mf" ]] || return 1
  local _body
  _body="$(cat "$_mf"; printf x)"
  HANDOFF_EXPECTED_LANE_TOKEN="${{_body%x}}"
}}
_release_lane_lease() {{
  [[ -n "${{HANDOFF_LANE_TOKEN:-}}" ]] || return 0
  "$CLOSER_NATIVE_LEASE_INTERPRETER" "$CLOSER_NATIVE_LEASE_PY" \\
    --handoff-root "$HANDOFF_HANDOFF_ROOT" release --lane "$lane" \\
    --expected-token "$HANDOFF_LANE_TOKEN" --lane-dir "$LANE_MUTEX_DIR" >/dev/null 2>&1 || true
}}
_owner_pid="$PPID"
_walk_pid="$$"
_hops=0
while (( _hops < 8 )); do
  _parent_pid="$(ps -o ppid= -p "$_walk_pid" 2>/dev/null || true)"
  _parent_pid="$(printf '%s' "$_parent_pid" | tr -d '[:space:]')"
  [[ "$_parent_pid" =~ ^[0-9]+$ ]] || break
  _comm="$(ps -o comm= -p "$_parent_pid" 2>/dev/null || true)"
  _comm="$(printf '%s' "$_comm" | tr -d '[:space:]')"
  _command="$(ps -o command= -p "$_parent_pid" 2>/dev/null || true)"
  _comm_base="${{_comm##*/}}"
  case "$_comm_base" in
    codex|claude|claude_code|cursor-agent|cursor|agent|muse|gemini)
      _owner_pid="$_parent_pid"; break ;;
    node|nodejs)
      if printf '%s\\n' "$_command" | grep -Eiq '(^|[^[:alnum:]_-])(codex|claude|cursor-agent|cursor|agent|muse|gemini)([^[:alnum:]_-]|$)'; then
        _owner_pid="$_parent_pid"; break
      fi ;;
  esac
  _walk_pid="$_parent_pid"
  _hops=$((_hops + 1))
done
[[ "$_owner_pid" =~ ^[0-9]+$ ]] || _owner_pid="$PPID"
_lease_refuse=0
[[ "${{HANDOFF_REFUSE_EXISTING_LANE:-0}}" == "1" ]] && _lease_refuse=1
set +e
_lease_token_out="$("$CLOSER_NATIVE_LEASE_INTERPRETER" "$CLOSER_NATIVE_LEASE_PY" \\
  --handoff-root "$HANDOFF_HANDOFF_ROOT" acquire --lane "$lane" \\
  --agent "$agent" --owner-pid "$_owner_pid" --lane-dir "$LANE_MUTEX_DIR" \\
  $([[ "$_lease_refuse" == 1 ]] && echo --refuse-existing-lane) 2>&1)"
_lease_rc=$?
set -e
if (( _lease_rc != 0 )); then
  if [[ "$_lease_token_out" == RELAY-HALTED:* ]]; then
    echo "${{_lease_token_out#RELAY-HALTED: }}"
  else
    echo "$_lease_token_out" >&2
  fi
  echo "RELAY-HALTED: lane '$lane' lease acquire failed"
  exit 1
fi
HANDOFF_LANE_TOKEN="$(printf '%s' "$_lease_token_out" | head -1 | tr -d '\\r')"
_read_lane_meta_credential || {{
  echo "handoff-lease: missing meta after acquire" >&2
  _release_lane_lease
  exit 1
}}
export HANDOFF_EXPECTED_LANE_TOKEN
if [[ "${{HANDOFF_PRERUN_MUTEX_ONLY:-0}}" == "1" ]]; then
  _release_lane_lease
  unset HANDOFF_LANE_TOKEN HANDOFF_EXPECTED_LANE_TOKEN
  exit 0
fi
{MARKER_END}
"""


def postrun_trap_replacement(*, repair_tail: bool = True) -> str:
    tail = "launch_keepalive_label_repair || _tail_rc=$?" if repair_tail else ":"
    return f"""{MARKER_START}
# HANDOFF_EXACT_TOKEN_V1: release only the original exact meta credential.
_release_lane_lease_postrun() {{
  : "${{CLOSER_NATIVE_LEASE_PY:?CLOSER_NATIVE_LEASE_PY must point at closer_native_lease.py}}"
  CLOSER_NATIVE_LEASE_INTERPRETER="${{CLOSER_NATIVE_LEASE_INTERPRETER:-python3}}"
  HANDOFF_HANDOFF_ROOT="${{HANDOFF_HANDOFF_ROOT:-$HOME/.codex/handoff}}"
  LANE_MUTEX_DIR="${{HANDOFF_LANE_MUTEX_DIR:-$HANDOFF_HANDOFF_ROOT/.lane-$lane.lock}}"
  local _tok="${{HANDOFF_EXPECTED_LANE_TOKEN:-}}"
  if [[ -z "$_tok" ]]; then
    echo "handoff-lease: release refused: missing original credential; lane retained" >&2
    return 2
  fi
  local _rc=0
  "$CLOSER_NATIVE_LEASE_INTERPRETER" "$CLOSER_NATIVE_LEASE_PY" \\
    --handoff-root "$HANDOFF_HANDOFF_ROOT" release --lane "$lane" \\
    --expected-token "$_tok" --lane-dir "$LANE_MUTEX_DIR" >/dev/null || _rc=$?
  if (( _rc != 0 )); then
    echo "handoff-lease: release refused rc=$_rc; lane retained" >&2
  fi
  return "$_rc"
}}
_finish_lane_postrun() {{
  local _body_rc="$1" _release_rc=0 _tail_rc=0
  trap - EXIT
  _release_lane_lease_postrun || _release_rc=$?
  {tail}
  (( _release_rc == 0 )) || exit "$_release_rc"
  (( _body_rc == 0 )) || exit "$_body_rc"
  exit "$_tail_rc"
}}
trap '_finish_lane_postrun "$?"' EXIT
{MARKER_END}"""


def patch_prerun(original: bytes, *, bounded_round: bool = False) -> bytes:
    text = original.decode("utf-8")
    if MARKER_START in text:
        if bounded_round and "CLOSER_NATIVE_ADMISSION_V1" not in text:
            _fail("bounded mode requires original canonical prerun input")
        return original
    if text.count(PRERUN_MUTEX_START) != 1:
        _fail("prerun mutex start anchor missing or ambiguous")
    start = text.index(PRERUN_MUTEX_START)
    line_end = text.find("\n", start)
    if line_end == -1 or "mutex" not in text[start:line_end]:
        _fail("prerun mutex start anchor malformed")
    if PRERUN_MUTEX_END not in text[start:]:
        _fail("prerun mutex end anchor missing after start")
    end = text.index(PRERUN_MUTEX_END, start)
    if not text.startswith("#!/bin/bash"):
        _fail("prerun must keep #!/bin/bash shebang")
    patched = text[:start] + lease_shell_block() + "\n\n" + text[end:]
    patched = patched.replace("_release_lane_mutex", "_release_lane_lease")
    if "_release_lane_mutex" in patched:
        _fail("prerun still references _release_lane_mutex")
    if 'rm -rf "$LANE_MUTEX_DIR"' in patched:
        _fail("prerun must not retain lane mutex rm -rf")
    if bounded_round:
        ordinary = '\n"$HANDOFF_BIN" pickup >/dev/null'
        review = '      "$HANDOFF_BIN" read-resume "$lane"\n      exit 0'
        if patched.count(ordinary) != 1 or patched.count(review) != 1:
            _fail("bounded mode requires exact canonical check-stop admission anchors")

        def verdict(state):
            return (
                "printf 'CLOSER_NATIVE_ADMISSION_V1 %s "
                + state
                + '\\n\' "${CLOSER_ADMISSION_NONCE:?}"\n'
            )

        patched = patched.replace(ordinary, "\n" + verdict("ordinary") + ordinary, 1)
        patched = patched.replace(review, "      " + verdict("review") + review, 1)
    return patched.encode("utf-8")


def patch_postrun(original: bytes, *, bounded_round: bool = False) -> bytes:
    text = original.decode("utf-8")
    if MARKER_START in text:
        if bounded_round and "CLOSER_BOUNDED_NO_RELAY_V1" not in text:
            _fail("bounded mode requires original canonical postrun input")
        return original
    matches = [anchor for anchor in POSTRUN_TRAP_ANCHORS if text.count(anchor) == 1]
    if len(matches) != 1:
        _fail("postrun mutex-release trap anchor missing or ambiguous")
    anchor = matches[0]
    if not text.startswith("#!/bin/bash"):
        _fail("postrun must keep #!/bin/bash shebang")
    patched = text.replace(
        anchor,
        postrun_trap_replacement(repair_tail="launch_keepalive_label_repair" in anchor),
        1,
    )
    if 'rm -rf "$LANE_MUTEX_DIR"' in patched:
        _fail("postrun must not retain lane mutex rm -rf trap")
    if bounded_round:
        gate = "# BEGIN CLOSER STATUS DISPATCHER STOP GATE v1"
        end = '"$HANDOFF_BIN" end-round "$outcome"'
        relay = 'if "$RELAY_BIN" "$relay_lane"; then'
        invocation = 'python3 "$_closer_dispatcher" validate-stop --state "$CLOSER_ROUND_STATE" --assignment "$HOME/.codex/automations/reviewed-repo-automation-controller/closer-assignment.json"'
        if any(patched.count(anchor) != 1 for anchor in (gate, end, invocation, relay)):
            _fail("bounded round requires exact standard validation/end-round anchors")
        patched = patched.replace(
            gate,
            """# CLOSER_BOUNDED_NO_RELAY_V1
# Dependency refusal still reaches the original exact-token EXIT cleanup.
if [[ "${CLOSER_POSTRUN_DEPENDENCIES_VALID:-0}" != "1" ]]; then
  echo "handoff-postrun: physical dependency pins changed; cleanup only" >&2
  exit 65
fi
""" + gate,
            1,
        )
        patched = patched.replace(
            invocation,
            '"${CLOSER_POSTRUN_PYTHON:?}" "${CLOSER_POSTRUN_DISPATCHER:?}" validate-stop --state "$CLOSER_ROUND_STATE" --assignment "${CLOSER_POSTRUN_ASSIGNMENT:?}"',
            1,
        )
        patched = patched.replace(
            relay,
            """
# Explicit one-round policy: normal stop validation and end-round ran above.
if [[ "${CLOSER_NO_NEW_RELAY:-0}" == "1" ]]; then
  echo "handoff-postrun: bounded round finalized; scheduled continuation retained"
  exit 0
fi
""" + relay,
            1,
        )
    return patched.encode("utf-8")


def render(
    prerun_src: Path,
    postrun_src: Path,
    output_dir: Path,
    *,
    bounded_round: bool = False,
) -> dict:
    prerun_bytes = prerun_src.read_bytes()
    postrun_bytes = postrun_src.read_bytes()
    out_prerun = patch_prerun(prerun_bytes, bounded_round=bounded_round)
    out_postrun = patch_postrun(postrun_bytes, bounded_round=bounded_round)
    output_dir.mkdir(parents=True, exist_ok=True)
    prerun_out = output_dir / "handoff-prerun.sh"
    postrun_out = output_dir / "handoff-postrun.sh"
    prerun_out.write_bytes(out_prerun)
    postrun_out.write_bytes(out_postrun)
    prerun_out.chmod(prerun_src.stat().st_mode)
    postrun_out.chmod(postrun_src.stat().st_mode)
    manifest = {
        "protocol": "HANDOFF_EXACT_TOKEN_V1",
        "bounded_no_new_relay": bounded_round,
        "pinned_native_hooks": {
            "lease_module": str(LEASE_MODULE),
            "lease_module_sha256": _sha(LEASE_MODULE.read_bytes()),
        },
        "sources": {
            "prerun": str(prerun_src),
            "prerun_sha256": _sha(prerun_bytes),
            "postrun": str(postrun_src),
            "postrun_sha256": _sha(postrun_bytes),
        },
        "outputs": {
            "prerun": str(prerun_out),
            "prerun_sha256": _sha(out_prerun),
            "postrun": str(postrun_out),
            "postrun_sha256": _sha(out_postrun),
        },
        "apply_plan": {
            "action": "preview_only",
            "install_targets": [
                "~/.codex/bin/handoff-prerun.sh",
                "~/.codex/bin/handoff-postrun.sh",
            ],
            "rollback": {
                "restore_prerun_sha256": _sha(prerun_bytes),
                "restore_postrun_sha256": _sha(postrun_bytes),
            },
        },
        "integration_gaps": [
            "Runtime must pin CLOSER_NATIVE_LEASE_PY and rendered script SHAs (PinnedNativeHooks).",
            "Legacy installed scripts keep stale reap / rm -rf until operator apply.",
            "Dispatcher/installer modules are not wired by this preview renderer.",
            "No live fleet tick or native queue validation in this checkout.",
        ],
    }
    (output_dir / "native-hooks-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render preview handoff native lease hooks")
    parser.add_argument("--prerun-source", type=Path, required=True)
    parser.add_argument("--postrun-source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--bounded-round", action="store_true")
    args = parser.parse_args(argv)
    for path in (args.prerun_source, args.postrun_source, LEASE_MODULE):
        if not path.is_file():
            print(f"missing source: {path}", file=sys.stderr)
            return 2
    manifest = render(
        args.prerun_source, args.postrun_source, args.output_dir, bounded_round=args.bounded_round
    )
    print(json.dumps(manifest["outputs"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
