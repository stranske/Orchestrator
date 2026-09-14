#!/usr/bin/env python3
"""Run committed read-only rail exercise contracts on a weekly shadow cadence."""

from __future__ import annotations

import argparse
import atexit
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import capabilities
import paths

# The CHECKOUT root, by the detected rule — never `parents[1]`. This module's first run from the
# flat exec mirror (the 00:40 UTC tick of 2026-09-14, the cadence's first run ever) resolved the
# tree to `~/.codex/tests/rail_exercises`, one level ABOVE the mirror, reported it absent by name,
# and stamped six days of success on a zero. `parents[1]` is right under `src/` and wrong on a flat
# tree; `paths.checkout_root` is right on both, which is the whole reason that module exists.
REPO_ROOT = paths.REPO_ROOT
CONTRACT_ROOT = REPO_ROOT / "tests" / "rail_exercises"
_EXEC_ROOT: Path | None = None


def exec_root() -> Path:
    """The directory contracts run in, and what `$REPO_ROOT` expands to.

    The committed contracts were written against a src/ checkout — `python3 src/<mod>.py`,
    `PYTHONPATH=src`, `cd $REPO_ROOT && ...` (33 of 49 say `src/` somewhere) — and the exec mirror
    is FLAT, so run there they fail on layout before they exercise anything. A src/ checkout is
    returned as itself. A flat tree gets a per-process VIEW: a temporary directory where `src`
    links to the module directory and every other top-level entry links to its original, so the
    shell sees the shape the contracts assume while the modules, which resolve `__file__` through
    the link, still see the real flat tree. Built once per process, removed at exit; contracts
    write only under their own sandboxed fixture copies, never under the view.
    """
    global _EXEC_ROOT
    if _EXEC_ROOT is None:
        if (REPO_ROOT / "src").is_dir():
            _EXEC_ROOT = REPO_ROOT
        else:
            view = Path(tempfile.mkdtemp(prefix="rail-exercise-view-"))
            for entry in REPO_ROOT.iterdir():
                if entry.name != "src":
                    (view / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
            (view / "src").symlink_to(paths.MODULE_DIR, target_is_directory=True)
            atexit.register(shutil.rmtree, view, True)
            _EXEC_ROOT = view
    return _EXEC_ROOT


CAPABILITY_ID = "rail-exercise-cadence"


def contracts(only: str = "") -> list[tuple[Path, dict[str, Any]]]:
    """Load every committed contract; a malformed one is reported, never discarded."""
    rows = []
    for path in contract_paths():
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            rows.append((path, {"_load_error": str(exc), "capability_id": path.parent.name}))
            continue
        if not only or row.get("capability_id") == only:
            rows.append((path, row))
    return rows


def _commands(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [item for item in value or [] if isinstance(item, str)]


def _expand(command: str, contract_dir: Path, fixture_dir: Path) -> str:
    """Expand source-contract paths without composing two absolute paths.

    The scratch contracts ran from an ``orch`` clone, where CONTRACT_DIR was
    a relative ``exercises``/``exercises2`` directory.  The port supplies an
    absolute disposable directory, so replace complete fixture expressions
    before the individual variables.  Otherwise ``$ROOT/$CONTRACT_DIR``
    becomes an invalid ``<root>/<absolute path>`` path.
    """
    for expression in (
        "$ROOT/$CONTRACT_DIR/fixtures",
        "$REPO_ROOT/$CONTRACT_DIR/fixtures",
        "$(pwd)/$CONTRACT_DIR/fixtures",
        "$CONTRACT_DIR/fixtures",
    ):
        command = command.replace(expression, str(fixture_dir))
    root = exec_root()
    command = (
        command.replace("$REPO_ROOT/orch", str(root))
        .replace("$CONTRACT_DIR", str(contract_dir))
        .replace("$REPO_ROOT", str(root))
        .replace("$FIXTURE_DIR", str(fixture_dir))
    )
    # r18 fixture builders were shipped as base64 Python snippets containing
    # their scratch clone's absolute fixture path.  Decode only that bounded
    # setup payload and retarget its fixture root before execution; otherwise
    # setup mutates the old scratch tree after this run has already copied its
    # inputs, producing order-dependent pass results.
    match = re.search(r"base64\.b64decode\('([A-Za-z0-9+/=]+)'\)", command)
    if not match:
        return command
    try:
        payload = base64.b64decode(match.group(1)).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return command

    def replacement(path_match: re.Match[str]) -> str:
        return f'Path(r"{fixture_dir / Path(path_match.group(1)).name}")'

    rewritten = re.sub(r'Path\(r"[^"\n]*/exercises2/fixtures/([^"\n]+)"\)', replacement, payload)
    if rewritten == payload:
        return command
    encoded = base64.b64encode(rewritten.encode("utf-8")).decode("ascii")
    return command[: match.start(1)] + encoded + command[match.end(1) :]


def _run(
    commands_: Any, *, contract_dir: Path, fixture_dir: Path, env: dict[str, str]
) -> list[dict]:
    output = []
    for command in _commands(commands_):
        expanded = _expand(command, contract_dir, fixture_dir)
        try:
            proc = subprocess.run(
                expanded,
                shell=True,
                cwd=exec_root(),
                env=env,
                text=True,
                capture_output=True,
                timeout=600,
            )
            output.append(
                {"command": command, "rc": proc.returncode, "output": proc.stdout + proc.stderr}
            )
        except subprocess.TimeoutExpired:
            output.append({"command": command, "rc": 124, "output": "timeout after 600 seconds"})
    return output


def _contract_name(path: Path) -> str:
    """Keep temporary selftest contracts nameable as well as committed ones."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _break_ok(case: dict[str, Any], result: list[dict], check: list[dict]) -> tuple[bool, str]:
    expected = str(
        case.get("expected")
        or case.get("expected_failure")
        or case.get("expected_failure_text")
        or ""
    )
    joined = "\n".join(item["output"] for item in [*result, *check])
    if check:
        return check[-1]["rc"] == 0, f"break check rc={check[-1]['rc']}"
    if expected and expected[:40] in joined:
        return True, "expected marker present"
    # A contract's explicit check is authoritative.  The remaining fallback
    # only accepts the declared literal marker; semantic word-token guessing
    # previously allowed the coordinator to misjudge a rail.
    return any(item["rc"] != 0 for item in result), "nonzero break run required"


def run_contract(path: Path, contract: dict[str, Any]) -> dict[str, Any]:
    capability_id = str(contract.get("capability_id") or path.parent.name)
    base = path.parent
    fixtures = base / "fixtures"
    if contract.get("_load_error"):
        return {
            "capability_id": capability_id,
            "contract": _contract_name(path),
            "status": "skip",
            "reason": contract["_load_error"],
        }
    if contract.get("skip_reason"):
        return {
            "capability_id": capability_id,
            "contract": _contract_name(path),
            "status": "skip",
            "reason": str(contract["skip_reason"]),
        }
    if not fixtures.is_dir():
        return {
            "capability_id": capability_id,
            "contract": _contract_name(path),
            "status": "skip",
            "reason": f"missing fixture directory: {fixtures}",
        }
    with tempfile.TemporaryDirectory(prefix="rail-exercise-") as temp:
        sandbox = Path(temp)
        # Preserve both source layouts used by the round-15 and round-16
        # contracts.  Several supplied harnesses derive ``orch/src`` and
        # ``orch/exercises/fixtures`` from their own location rather than
        # accepting a fixture argument, so a flat temporary ``fixtures``
        # directory is not a faithful port.  The second name is a SYMLINK,
        # never a second copy: a contract's setup mutates ``$CONTRACT_DIR``
        # (stall-watcher ages its log with ``touch -t``), and a harness that
        # reads the other name must see that mutation.  Two copies made the
        # stale-log arm pass only on a working tree whose mtime had been set
        # by hand — which git does not store, so every fresh checkout failed.
        shadow_root = sandbox / "orch"
        shadow_root.mkdir()
        (shadow_root / "src").symlink_to(paths.MODULE_DIR, target_is_directory=True)
        contract_dir = shadow_root / "exercises2"
        copied = contract_dir / "fixtures"
        shutil.copytree(fixtures, copied)
        (shadow_root / "exercises").symlink_to(contract_dir, target_is_directory=True)
        state = sandbox / "state"
        runtime = sandbox / "runtime"
        state.mkdir()
        runtime.mkdir()
        env = {**os.environ, "ORCH_STATE_DIR": str(state), "ORCH_LOCAL_RUNTIME": str(runtime)}
        env.pop("ORCH_CAPABILITY_HEARTBEATS", None)
        setup = _run(contract.get("setup"), contract_dir=contract_dir, fixture_dir=copied, env=env)
        run = _run(contract.get("run"), contract_dir=contract_dir, fixture_dir=copied, env=env)
        passed = _run(
            contract.get("pass_check"), contract_dir=contract_dir, fixture_dir=copied, env=env
        )
        case = contract.get("break_case")
        if not isinstance(case, dict) or not _commands(case.get("run") or case.get("command")):
            return {
                "capability_id": capability_id,
                "contract": _contract_name(path),
                "status": "skip",
                "reason": "no runnable break case",
                "pass_rc": passed[-1]["rc"] if passed else None,
            }
        break_setup = _run(
            case.get("setup"), contract_dir=contract_dir, fixture_dir=copied, env=env
        )
        broken = _run(
            case.get("run") or case.get("command"),
            contract_dir=contract_dir,
            fixture_dir=copied,
            env=env,
        )
        checked = _run(
            case.get("pass_check") or case.get("check"),
            contract_dir=contract_dir,
            fixture_dir=copied,
            env=env,
        )
        pass_rc = passed[-1]["rc"] if passed else 99
        break_ok, break_reason = _break_ok(case, broken, checked)
        status = "pass" if pass_rc == 0 and break_ok else "fail"
        return {
            "capability_id": capability_id,
            "contract": _contract_name(path),
            "status": status,
            "pass_rc": pass_rc,
            "break_ok": break_ok,
            "reason": break_reason,
            "commands": {
                "setup": setup,
                "run": run,
                "pass_check": passed,
                "break_setup": break_setup,
                "break_run": broken,
                "break_check": checked,
            },
        }


def _record(row: dict[str, Any]) -> str:
    """Record the old wave's trigger + machine-observed verdict only when explicitly armed."""
    try:
        import capability_advisor
        import capability_propensity

        phases = [
            phase
            for phase in capability_advisor.surfaces_binding([row["capability_id"]]).get(
                row["capability_id"], []
            )
            if phase.startswith("rail-exercise:")
        ]
        if len(phases) != 1:
            return f"record failed: expected one rail-exercise phase, found {phases}"
        phase = phases[0]
        advice = capability_advisor.advise(
            f"rail exercise for {row['capability_id']}",
            surface=phase,
            repository="stranske/Orchestrator",
        )
        experiment = str(advice.get("experiment_id") or "")
        if not experiment:
            return "record failed: advisor returned no experiment id"
        capability_propensity.record_trigger(
            row["capability_id"], experiment, metadata={"surface": phase}
        )
        capability_propensity.record_usefulness(
            row["capability_id"],
            experiment,
            useful=row["status"] == "pass",
            evidence=row["reason"],
            provenance="machine_observed",
            judge="rail-exercise cadence",
        )
        return "recorded"
    except Exception as exc:  # evidence failure must be visible, not turn a contract green
        return f"record failed: {exc}"


def report(only: str = "", record: bool = False) -> dict[str, Any]:
    tree = "present" if CONTRACT_ROOT.exists() else f"absent: {CONTRACT_ROOT}"
    rows = [run_contract(path, contract) for path, contract in contracts(only)]
    for row in rows:
        if record and row["status"] != "skip":
            row["record"] = _record(row)
    totals = {
        "contracts": len(rows),
        "passed": sum(r["status"] == "pass" for r in rows),
        "broke-correctly": sum(r.get("break_ok") is True for r in rows),
        "failed": sum(r["status"] == "fail" for r in rows),
        "skipped": sum(r["status"] == "skip" for r in rows),
    }
    return {
        "generated_at": int(time.time()),
        "recording": record,
        "tree": tree,
        "totals": totals,
        "skipped_named": [
            {"contract": r["contract"], "reason": r["reason"]}
            for r in rows
            if r["status"] == "skip"
        ],
        "contracts": rows,
    }


def contract_paths(root: Path | None = None) -> list[Path]:
    """Every contract under the tree, at any depth — the runner and the guards share this so a
    contract the guards cannot see is impossible (they found 42 of 49 with a fixed-depth glob)."""
    root = CONTRACT_ROOT if root is None else root
    return sorted(root.glob("**/contract.json"))


_REDIRECT = re.compile(r">\s*\$CONTRACT_DIR/([^\s;|&)'\"]+)")


def committed_run_outputs(root: Path | None = None) -> list[str]:
    """Files at rest that a contract's run (or break run) would write.

    The runner copies fixtures before running, so a committed run output is copied too — and a
    pass check reading it succeeds even when the run itself failed. Setup-written inputs are
    fine (setup regenerates them); only run redirects count.
    """
    root = CONTRACT_ROOT if root is None else root
    found: list[str] = []
    for contract in contract_paths(root):
        body = json.loads(contract.read_text())
        commands: list[str] = []
        for value in (body.get("run"), (body.get("break_case") or {}).get("run")):
            if isinstance(value, list):
                commands.extend(value)
            elif isinstance(value, str):
                commands.append(value)
        for command in commands:
            for match in _REDIRECT.finditer(command):
                target = contract.parent / match.group(1)
                if target.exists():
                    found.append(str(target.relative_to(root)))
    return found


_UNSYNCABLE_PART = re.compile(r'[<>:"|?*\\]')


def unsyncable_paths(root: Path | None = None) -> list[str]:
    """Committed paths a Dropbox checkout cannot hold: a component with leading or trailing
    whitespace, a trailing dot, or one of `<>:"|?*\\`. Seven contracts carried a duplicate fixture
    directory named `capability-activation-audit ` (trailing space; PR #207). The owner's Dropbox
    checkout collapsed the two names into "conflicted copy" files, then deleted the live `run.py`
    beside each one, and a sync from that working tree would have shipped the damage. Reported by
    name; the selftest refuses a committed tree that has any."""
    root = CONTRACT_ROOT if root is None else root
    found = []
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if any(
            part != part.strip() or part.endswith(".") or _UNSYNCABLE_PART.search(part)
            for part in parts
        ):
            found.append(str(path.relative_to(root)))
    return found


def nested_repositories(root: Path | None = None) -> list[str]:
    """`.git` entries under the contract tree. A fixture repository is built at run time, never
    committed: git records a nested repository as a gitlink with no URL, and every CI checkout
    then fails at `git submodule foreach` (PR #207's first run)."""
    root = CONTRACT_ROOT if root is None else root
    return sorted(str(p.relative_to(root)) for p in root.rglob(".git"))


def selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="rail-exercise-selftest-") as temp:
        root = Path(temp)
        original = CONTRACT_ROOT
        globals()["CONTRACT_ROOT"] = root
        try:
            for name, body in {
                "good": {"run": "true", "pass_check": "true", "break_case": {"run": "false"}},
                "bad-break": {"run": "true", "pass_check": "true", "break_case": {"run": "true"}},
                "missing": {"run": "true", "pass_check": "true", "break_case": {"run": "false"}},
            }.items():
                folder = root / name
                folder.mkdir()
                (folder / "contract.json").write_text(json.dumps({"capability_id": name, **body}))
                if name != "missing":
                    (folder / "fixtures").mkdir()
            rep = report()
            by_id = {r["capability_id"]: r for r in rep["contracts"]}
            assert by_id["good"]["status"] == "pass", by_id
            assert by_id["bad-break"]["status"] == "fail", by_id
            assert by_id["missing"]["status"] == "skip", by_id
        finally:
            globals()["CONTRACT_ROOT"] = original
        # The guards themselves, on a tree built to trip both.
        bad = root / "tripwire" / "arm-a"
        (bad / "fixtures" / ".git").mkdir(parents=True)
        (bad / "fixtures" / "out.json").write_text("{}")
        (bad / "contract.json").write_text(
            json.dumps(
                {"capability_id": "tripwire", "run": "true > $CONTRACT_DIR/fixtures/out.json"}
            )
        )
        assert nested_repositories(root) == ["tripwire/arm-a/fixtures/.git"], nested_repositories(
            root
        )
        assert committed_run_outputs(root) == [
            "tripwire/arm-a/fixtures/out.json"
        ], committed_run_outputs(root)
        (bad / "fixtures" / "trailing ").mkdir()
        assert unsyncable_paths(root) == ["tripwire/arm-a/fixtures/trailing "], unsyncable_paths(
            root
        )
        absent_root = Path(temp) / "missing-contract-tree"
        globals()["CONTRACT_ROOT"] = absent_root
        try:
            absent_rep = report()
            assert absent_rep["tree"] == f"absent: {absent_root}", absent_rep
            assert absent_rep["totals"]["contracts"] == 0, absent_rep
        finally:
            globals()["CONTRACT_ROOT"] = original
    # The committed tree: no nested repository, no run output at rest.
    if CONTRACT_ROOT.exists():
        nested = nested_repositories()
        assert not nested, f"fixture repositories are built at run time, never committed: {nested}"
        stale = committed_run_outputs()
        assert (
            not stale
        ), f"a committed run output lets a pass check succeed without the run: {stale}"
        unsyncable = unsyncable_paths()
        assert not unsyncable, f"a Dropbox checkout cannot hold these committed paths: {unsyncable}"
        tree = f"committed tree clean ({len(contract_paths())} contracts)"
    else:
        tree = f"committed tree absent at {CONTRACT_ROOT} — not checked"
    print(
        "rail_exercise.py selftest: OK (pass, broken break case, named missing-fixture skip, "
        f"tripwire guards incl. unsyncable names; {tree})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument(
        "--record", action="store_true", help="write explicit machine-observed exercise evidence"
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return 0
    result = report(args.only, args.record)
    # Credit the CADENCE on its executed path. orchestrate.sh runs this module as a script, so
    # main() IS the path (capability_activation_audit.heartbeat_reachable credits the declared
    # entrypoint `rail_exercise.py:main`); daily coalescing because a weekly step needs no more.
    # Never inside report(): the selftest calls report() on a temporary tree and must not credit.
    try:
        totals = result["totals"]
        # The literal id, not CAPABILITY_ID: test_every_capability_has_a_heartbeat_call_site greps a
        # five-line window after `heartbeat(` for the quoted id, and a constant hides the wiring
        # from the very check that proves it. CI never saw this red — it runs on an empty ledger.
        capabilities.daily_heartbeat(
            "rail-exercise-cadence",
            "success" if totals.get("failed", 0) == 0 else "failure",
            ref="rail_exercise.main",
            metadata={key: int(value) for key, value in totals.items()},
        )
    except Exception as exc:  # noqa: BLE001 — a swallowed heartbeat reads as dormancy later
        print(f"rail_exercise: capability heartbeat failed: {exc}", file=sys.stderr)
    print(
        json.dumps(result, indent=2, sort_keys=True)
        if args.json
        else json.dumps({**result["totals"], "tree": result["tree"]}, sort_keys=True)
    )
    # Zero contracts — the tree absent, or present and empty — is a FAILED run. orchestrate.sh
    # stamps success on exit 0 and then waits six days; a stamped zero is exactly the latched
    # silence this cadence exists to break, and the mirror's first run produced one. Non-zero
    # here means `_mark_fail`: retry in six hours, ALERT after three, and the report still names
    # the tree, so the reader sees WHY rather than a missing file.
    if result["totals"]["contracts"] == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
