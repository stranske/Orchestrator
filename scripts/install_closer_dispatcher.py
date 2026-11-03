#!/usr/bin/env python3
"""Preview or explicitly install the local closer prompt and postrun stop gate.

The local TOML and shell script are the existing canonical runtime sources.
This installer owns only its marked additions and exact obsolete quota text.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_START = "<!-- CLOSER STATUS DISPATCHER POLICY v1 -->"
POLICY_END = "<!-- END CLOSER STATUS DISPATCHER POLICY -->"
GATE_START = "# BEGIN CLOSER STATUS DISPATCHER STOP GATE v1"
GATE_END = "# END CLOSER STATUS DISPATCHER STOP GATE"
TRAP_ANCHOR = "trap 'rm -rf \"$LANE_MUTEX_DIR\" 2>/dev/null || true' EXIT"
TRAP_ANCHORS = (
    TRAP_ANCHOR,
    TRAP_ANCHOR[: -len("' EXIT")] + "; launch_keepalive_label_repair' EXIT",
)
PROMPT_TOKEN = re.compile(
    r'''(?m)^prompt\s*=\s*(?P<value>"""[\s\S]*?"""|'''
    + "'''[\\s\\S]*?'''"
    + r"""|"(?:\\.|[^"\\])*"|'[^'\n]*')"""
)
SUBSTITUTIONS = {
    "Then select **one complex lane**": "Then select **eligible lifecycle work**",
    "select at most one complex repo lane per run.": "continue eligible lifecycle work across repositories within the actual resource boundary.",
    "then at most one complex lane.": "then continue eligible lifecycle work within the actual resource boundary.",
    "as the one complex lane,": "for lifecycle work,",
    "the one-complex-lane phase": "status-driven lifecycle selection",
    "the batch sweep plus one complex lane": "the status-driven eligible queue",
    "the one complex lane produce": "the selected lifecycle work produce",
    "for the one complex lane.": "for each selected lifecycle target.",
    "selected one complex lane": "selected eligible lifecycle work",
}


def stop_gate(enforce: bool) -> str:
    required = "1" if enforce else "0"
    return f"""{GATE_START}
# Validate before end-round or any relay; existing EXIT cleanup still releases the mutex.
# Migration permits an already-running old-prompt round to finish without a ledger.
if [[ "$lane" == "closer" ]]; then
  if [[ -n "${{CLOSER_ROUND_STATE:-}}" ]]; then
    _closer_dispatcher="$HOME/.codex/orchestrator-mirror/closer_dispatcher.py"
    if [[ ! -f "$_closer_dispatcher" ]]; then
      echo "handoff-postrun: missing closer dispatcher; round not finalized" >&2
      exit 65
    fi
    if ! python3 "$_closer_dispatcher" validate-stop --state "$CLOSER_ROUND_STATE" --assignment "$HOME/.codex/automations/reviewed-repo-automation-controller/closer-assignment.json"; then
      echo "handoff-postrun: closer stop rejected; preserve ledger and resume actionable work" >&2
      exit 65
    fi
  elif [[ "{required}" == "1" ]]; then
    echo "handoff-postrun: CLOSER_ROUND_STATE required; round not finalized" >&2
    exit 65
  else
    echo "handoff-postrun: migration pending: old-prompt closer round has no CLOSER_ROUND_STATE" >&2
  fi
fi
{GATE_END}
"""


def updated_prompt(original: bytes, policy: str) -> bytes:
    text = original.decode("utf-8")
    before = tomllib.loads(text)
    prompt = before["prompt"]
    if POLICY_START in prompt:
        start = prompt.index(POLICY_START)
        end = prompt.index(POLICY_END, start) + len(POLICY_END)
        prompt = (prompt[:start] + prompt[end:]).lstrip("\n")
    for old, new in SUBSTITUTIONS.items():
        prompt = prompt.replace(old, new)
    prompt = f"{POLICY_START}\n{policy.strip()}\n{POLICY_END}\n\n{prompt}"
    matches = list(PROMPT_TOKEN.finditer(text))
    if len(matches) != 1:
        raise ValueError("expected exactly one top-level prompt token")
    match = matches[0]
    result = text[: match.start("value")] + json.dumps(prompt) + text[match.end("value") :]
    after = tomllib.loads(result)
    expected = dict(before, prompt=prompt)
    if after != expected:
        raise ValueError("non-prompt TOML data changed")
    return result.encode("utf-8")


def updated_postrun(original: bytes, *, enforce: bool) -> bytes:
    text = original.decode("utf-8")
    if GATE_START in text:
        start = text.index(GATE_START)
        end = text.index(GATE_END, start) + len(GATE_END)
        # A default reinstall cannot silently downgrade an enforced gate.
        enforce = enforce or 'elif [[ "1" == "1" ]]' in text[start:end]
        if text[start - 2 : start] != "\n\n" or text[end : end + 1] != "\n":
            raise ValueError("installed stop gate delimiters changed")
        text = text[: start - 2] + text[end + 1 :]
    anchors = [anchor for anchor in TRAP_ANCHORS if text.count(anchor) == 1]
    if len(anchors) != 1 or sum(text.count(anchor) for anchor in TRAP_ANCHORS) != 1:
        raise ValueError("postrun mutex-release anchor changed; inspect before installing")
    anchor = anchors[0]
    return text.replace(anchor, anchor + "\n\n" + stop_gate(enforce)).encode("utf-8")


def atomic_write(path: Path, content: bytes, expected: bytes | None) -> None:
    """Refuse stale inputs; use same-directory atomic replacement preserving mode."""
    current = path.read_bytes() if path.exists() else None
    if current != expected:
        raise RuntimeError(f"concurrent source change: {path}")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        # Recheck after preparation; a cooperative installer also serializes externally.
        if (path.read_bytes() if path.exists() else None) != expected:
            raise RuntimeError(f"concurrent source change: {path}")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _install(home: Path, *, apply: bool = False, enforce: bool = False) -> dict:
    """Apply only explicit source changes, restoring our writes on rendering failure."""
    prompt = home / ".codex/automations/imi-merge-verify-closer/automation.toml"
    postrun = home / ".codex/bin/handoff-postrun.sh"
    renderer = home / ".codex/bin/render-claude-prompts.sh"
    renderers = {"claude": renderer}
    for family in ("cursor", "muse"):
        optional = home / f".codex/bin/render-{family}-prompts.sh"
        if optional.exists():
            renderers[family] = optional
    policy = (ROOT / "docs/closer-dispatcher-policy.md").read_text()
    snapshots = {path: path.read_bytes() for path in (prompt, postrun)}
    replacements = {
        prompt: updated_prompt(snapshots[prompt], policy),
        postrun: updated_postrun(snapshots[postrun], enforce=enforce),
    }
    changed = [path for path in replacements if replacements[path] != snapshots[path]]
    effective_enforce = 'elif [[ "1" == "1" ]]' in replacements[postrun].decode()
    receipt = {
        "mode": "apply" if apply else "dry-run",
        "stop_enforcement": "required" if effective_enforce else "migration",
        "changes": [str(path) for path in changed],
        "original_sha256": {str(p): hashlib.sha256(b).hexdigest() for p, b in snapshots.items()},
    }
    if not apply:
        return receipt
    for family, script in renderers.items():
        if not script.is_file() or not os.access(script, os.X_OK):
            raise ValueError(f"normal {family} prompt renderer unavailable")
    dispatcher = home / ".codex/orchestrator-mirror/closer_dispatcher.py"
    if not dispatcher.is_file():
        raise ValueError(
            "deploy repository src/closer_dispatcher.py through normal mirror sync first"
        )
    stamp = f"{time.time_ns()}-{os.getpid()}"
    caches = [
        home / f".codex/handoff/prompts/{family}-{lane}.md"
        for family in renderers
        for lane in ("opener", "closer")
    ]
    cache_snapshots = {p: p.read_bytes() if p.exists() else None for p in caches}
    expected_caches = {}
    for family in renderers:
        for lane in ("opener", "closer"):
            path = home / f".codex/handoff/prompts/{family}-{lane}.md"
            source = (
                home
                / f".codex/automations/{'pd-workloop-resume' if lane == 'opener' else 'imi-merge-verify-closer'}/automation.toml"
            )
            body = replacements[prompt] if lane == "closer" else source.read_bytes()
            value = tomllib.loads(body.decode())["prompt"].replace(
                "HANDOFF_AGENT=codex",
                f"HANDOFF_AGENT={'claude_code' if family == 'claude' else family}",
            )
            header = (
                (
                    f"<!-- AUTO-GENERATED from {source} by render-claude-prompts.sh — DO NOT EDIT. "
                    "Regenerated from the TOML (the single source of truth) at the start of every Claude "
                    "scheduled round; hand edits here are overwritten. Edit the prompt in the TOML. -->\n\n"
                )
                if family == "claude"
                else ""
            )
            expected_caches[path] = (header + value).encode()
    if not changed and cache_snapshots == expected_caches:
        receipt["rendered"] = False
        return receipt
    written = []
    try:
        for path in changed:
            # Backups contain the exact pre-install source and remain available after rollback.
            backup = path.with_name(path.name + ".closer-dispatcher-backup-" + stamp)
            with backup.open("xb") as stream:
                stream.write(snapshots[path])
            os.chmod(backup, 0o600)
            atomic_write(path, replacements[path], snapshots[path])
            written.append(path)
        for script in renderers.values():
            subprocess.run(
                [str(script)], env=dict(os.environ, HOME=str(home)), check=True, capture_output=True
            )
        if any(
            not path.exists() or path.read_bytes() != content
            for path, content in expected_caches.items()
        ):
            raise RuntimeError("normal renderer did not produce the expected source-derived caches")
    except Exception as exc:
        conflicts = []
        for path in reversed(written):
            try:
                atomic_write(path, snapshots[path], replacements[path])
            except RuntimeError:
                conflicts.append(str(path))
        # Renderer owns these generated caches; do not overwrite a concurrent unrelated edit.
        for path, content in cache_snapshots.items():
            current = path.read_bytes() if path.exists() else None
            if current == content:
                continue
            if current != expected_caches[path]:
                conflicts.append(str(path))
                continue
            if content is None:
                path.unlink()
            else:
                atomic_write(path, content, expected_caches[path])
        if conflicts:
            raise RuntimeError(
                f"rollback refused concurrent changes at {conflicts}; backups retained"
            ) from exc
        raise
    receipt["rendered"] = True
    receipt["rendered_families"] = list(renderers)
    return receipt


def install(home: Path, *, apply: bool = False, enforce: bool = False) -> dict:
    """Serialize cooperating installers; ordinary source edits are also compared before replace."""
    if not apply:
        return _install(home, enforce=enforce)
    lock_path = home / ".codex/bin/.install-closer-dispatcher.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another closer dispatcher installation is active") from exc
        return _install(home, apply=True, enforce=enforce)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--enforce-stop",
        action="store_true",
        help="Require state only after old rounds are terminal",
    )
    args = parser.parse_args()
    print(json.dumps(install(args.home, apply=args.apply, enforce=args.enforce_stop), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
