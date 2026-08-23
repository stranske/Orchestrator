#!/usr/bin/env python3
"""env_prereq.py — "is the thing this check needs actually HERE?", answered BY NAME.

WHY THIS EXISTS. The first CI run of the public repo (2026-08-21) was red: 21 pytest
failures, 8 module selftests and 2 capability gates, against 330 green on the owner's
machine. Not one was a defect in this tree. Each check needed something that exists only on
the machine the system runs on, and a GitHub runner has none of it: no agent CLIs, no
`~/.codex/skills`, no `/Applications/ChatGPT.app`, and — the big one — no capability ledger.
The ledger is machine-local state by design (`$ORCH_STATE_DIR`, never committed); a fresh
bootstrap holds the 14 rows the code declares, while this instance's ledger holds 40. The
other 26 are accumulated registration history and cannot be reconstructed from source.

So the tree was fine and the instrument said RED. The opposite error — declaring green while
running less — is the failure this project is named for, so the fix could not be a blanket
skip, a try/except, or a narrower CI invocation. What it is instead:

  * A check whose prerequisite is genuinely absent SKIPS, and its skip carries a reason that
    NAMES the missing thing. `MissingPrerequisite` subclasses `unittest.SkipTest`, which
    pytest reports as SKIPPED with the message from a test body, a fixture, or a module-level
    `skipif`; the dual-mode `main()` runners in `test_capability_admission.py` and
    `test_capability_set_coverage.py` catch the same exception explicitly. One exception type,
    one reason string, every harness.
  * Detection is of the PREREQUISITE, never of CI. Nothing here reads `$CI` or
    `$GITHUB_ACTIONS`: the question is "does this binary exist", "does this ledger row carry
    version lineage" — so the same code gives the right answer on a runner, on the owner's
    box, and on a second instance with a different `ORCH_STATE_DIR`.
  * Skipping is BOUNDED, not open-ended. `verify.py` enforces a ceiling on the number of
    skipped tests, skipped selftests and skipped gates (`.verify-floor.json`), and prints
    every skip reason, so "green" always states what did not run. A future change that skips
    more than the agreed set FAILS.

Assertions are untouched. A check that RUNS still asserts exactly what it asserted before;
only its applicability gate is new.

DEDUP FINDING (CLAUDE.md §0), recorded 2026-08-21 before writing a line. Grepped the tree for
the concept, not the name: `pytest.skip` appears in exactly one file
(`test_model_tier_resolution.py`, twice, both `shutil.which`-gated — the idiom this module
generalises); there is no `conftest.py`, no `pytest.ini`/`pyproject.toml`, and no shared
prerequisite/applicability helper of any kind. The nearest relatives are single-call-site
degradations, not reusable machinery: `capability_admission.py:335` ("cannot judge without the
ledger; never fail on absence") and `capability_activation_audit._fleet_label_index`, which
skips a repo whose `gh` call fails. Nothing to wire, activate or un-gate — this concept is
genuinely absent, so it is new. This module is test-applicability infrastructure, not an
orchestrator capability: it has no dispatch path, no outcome and no ledger row, so the
admission gate does not bind on it.
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

# The single token verify.py greps for in a selftest's or gate's output to classify it as
# SKIPPED rather than passed. Defined once here and consumed by both sides, so the writer and
# the reader cannot drift apart.
PREREQ_ABSENT_MARK = "PREREQUISITE ABSENT:"


class MissingPrerequisite(unittest.SkipTest):
    """This check cannot apply here, and the message says what is missing.

    Subclasses `unittest.SkipTest` on purpose: pytest natively reports it as a skip carrying
    the reason, and a plain `except MissingPrerequisite` works in the hand-rolled runners. It
    is NOT an assertion failure and must never be raised to paper over one — the prerequisite
    is a fact about the machine, never about the code under test.
    """


# --------------------------------------------------------------------------- the ledger
# The capability ledger is machine-local accumulated history. Three distinct facets of its
# absence produce three distinct failures, so each gets its own named detector rather than one
# vague "ledger looks empty".


def _ledger() -> dict:
    # Imported lazily: `capabilities` imports `feedback`, and `feedback`'s own selftest imports
    # this module — a module-level import here would close that cycle.
    import capabilities

    return capabilities.load_declared(capabilities.REG)


def ledger_rows_absent(*capability_ids: str) -> str | None:
    """Reason string when any named capability has no row in the ledger at all."""
    try:
        ledger = _ledger()
    except Exception as exc:  # noqa: BLE001
        return f"capability ledger unreadable ({type(exc).__name__}: {exc})"
    missing = sorted(c for c in capability_ids if c not in ledger)
    if not missing:
        return None
    import capabilities

    return (
        f"capability ledger has no row for {', '.join(missing)} — the ledger is "
        f"machine-local state ({capabilities.REG}); it holds {len(ledger)} row(s) here, "
        f"and these are registered by running the system, not by checking out the tree"
    )


def ledger_version_lineage_absent(*capability_ids: str) -> str | None:
    """Reason string when a row exists but carries no `capability_version_id`.

    A freshly bootstrapped row has `capability_version_id: None`, and the influence-edge writer
    refuses an edge without lineage — by design, so a capability cannot be credited with a
    version it never had. That refusal is correct behaviour and not something to assert around.
    """
    try:
        ledger = _ledger()
    except Exception as exc:  # noqa: BLE001
        return f"capability ledger unreadable ({type(exc).__name__}: {exc})"
    unversioned = sorted(
        c for c in capability_ids if not (ledger.get(c) or {}).get("capability_version_id")
    )
    if not unversioned:
        return None
    return (
        f"capability ledger carries no version lineage for {', '.join(unversioned)} "
        f"(capability_version_id is unset) — lineage is established by real registration "
        f"on the running instance, so a fresh bootstrap has none"
    )


def ledger_invocation_history_absent(*capability_ids: str) -> str | None:
    """Reason string when a row has never recorded an invocation on this machine."""
    try:
        ledger = _ledger()
    except Exception as exc:  # noqa: BLE001
        return f"capability ledger unreadable ({type(exc).__name__}: {exc})"
    silent = sorted(c for c in capability_ids if not (ledger.get(c) or {}).get("last_invocation"))
    if not silent:
        return None
    return (
        f"capability ledger records no invocation for {', '.join(silent)} "
        f"(last_invocation is unset) — liveness classification reads that history, which "
        f"only accrues on the running instance"
    )


def ledger_legacy_rows_absent() -> str | None:
    """Reason string when the ledger holds no capability registered before the admission gate.

    `capability_admission` scopes enforcement to capabilities registered from 2026-08-21 and
    reports the earlier ones as legacy debt. A ledger bootstrapped today has no earlier ones,
    so "legacy debt is reported, not forgiven" has nothing to report on.
    """
    try:
        import capability_admission as admission

        rows = admission.report()["rows"]
    except Exception as exc:  # noqa: BLE001
        return f"admission report unavailable ({type(exc).__name__}: {exc})"
    if any(r.get("legacy") for r in rows):
        return None
    return (
        "capability ledger holds no pre-admission-gate (legacy) capability — legacy debt "
        "accrues from this instance's registration history, and a ledger bootstrapped "
        "from the committed tree has none"
    )


# --------------------------------------------------------------------------- local files & CLIs


def skill_resource_absent() -> str | None:
    """Reason string when the reference skill's bundled script is not installed.

    `capability_compiler.reference_skill_source()` hashes a real installed skill resource under
    `~/.codex/skills`; the compiler is exercised against a genuine file on purpose, so there is
    nothing to compile when the skill is not installed.
    """
    import capability_compiler

    try:
        resource = Path(capability_compiler.reference_skill_source()["resources"][0]["source_path"])
    except (OSError, KeyError, IndexError):
        # reference_skill_source() hashes the file as it builds the dict, so an absent resource
        # raises here — which is the answer, not an error.
        resource = (
            Path.home()
            / ".codex"
            / "skills"
            / "code-workspace-hygiene"
            / "scripts"
            / "audit_code_root.sh"
        )
    if resource.is_file():
        return None
    return (
        f"reference skill resource not installed: {resource} — the skill compiler is "
        f"deliberately exercised against a real installed skill, not a fixture"
    )


def codex_profile_binary_absent() -> str | None:
    """Reason string when the version-capable Codex binary exact profiles require is absent.

    `adapters.profile_codex_binary()` fails closed rather than falling back to whatever `codex`
    is on PATH — the whole point of an exact profile is that the binary can pin a version. The
    default location is inside a macOS app bundle, so it cannot exist on a Linux runner.
    """
    import adapters

    if adapters.CODEX_PROFILE_BIN.is_file():
        return None
    return (
        f"exact-profile Codex binary absent: {adapters.CODEX_PROFILE_BIN} "
        f"(set ORCH_CODEX_PROFILE_BIN to a version-capable Codex binary) — "
        f"adapters.profile_codex_binary() fails closed rather than using PATH"
    )


def agent_cli_absent(*agents: str) -> str | None:
    """Reason string when a seat's CLI is not installed, so its auth probe cannot run."""
    import adapters

    missing = []
    for agent in agents:
        probe = (adapters.AUTH_PROBES.get(agent) or {}).get("cmd") or []
        binary = probe[0] if probe else agent
        if not shutil.which(str(binary)):
            missing.append(f"{agent} ({binary})")
    if not missing:
        return None
    return (
        f"agent CLI not installed for {', '.join(missing)} — with no CLI and no credential "
        f"file there is genuinely no free signal for the seat, which is the documented "
        f"UNKNOWN case, not a broken credential"
    )


def credential_file_absent(*agents: str) -> str | None:
    """Reason string when the credential FILE the fleet sources at dispatch time is absent."""
    import agent_auth_check

    missing = []
    for agent in agents:
        entry = agent_auth_check.CREDENTIAL_FILES.get(agent)
        if entry is None:
            missing.append(f"{agent} (no credential file registered)")
        elif not entry[0].is_file():
            missing.append(f"{agent} ({entry[0]})")
    if not missing:
        return None
    return (
        f"credential file absent for {', '.join(missing)} — the fleet authenticates by "
        f"sourcing that file, so without it the seat's verdict is BROKEN by design"
    )


def seat_has_no_free_signal() -> str | None:
    """Reason string naming every seat that has neither an installed CLI nor a credential file.

    Gate for the "no seat may report UNKNOWN" check. A seat with no probe and no credential
    file has, by `agent_auth_check`'s own documented rule, no free signal at all — and UNKNOWN
    is explicitly never treated as a failure there. Asserting the absence of UNKNOWN on such a
    machine tests the machine, not the code.
    """
    import adapters
    import agent_auth_check

    blind = []
    for agent in agent_auth_check.AGENTS:
        probe = (adapters.AUTH_PROBES.get(agent) or {}).get("cmd") or []
        binary = str(probe[0]) if probe else None
        has_cli = bool(binary and shutil.which(binary))
        entry = agent_auth_check.CREDENTIAL_FILES.get(agent)
        has_file = bool(entry and entry[0].is_file())
        if not has_cli and not has_file:
            blind.append(agent)
    if not blind:
        return None
    return (
        f"no free auth signal on this machine for {', '.join(sorted(blind))}: neither an "
        f"installed CLI probe nor a credential file — agent_auth_check's documented "
        f"UNKNOWN case"
    )


# --------------------------------------------------------------------------- harness glue


def require(*reasons: str | None) -> None:
    """Raise `MissingPrerequisite` for the first named absence, or return.

    Call with detector results: `require(ledger_rows_absent("issue-readiness"))`.
    """
    for reason in reasons:
        if reason:
            raise MissingPrerequisite(reason)


def selftest_skipped(module: str, *reasons: str | None) -> bool:
    """For a module `--selftest`: print the marked reason and report that it did not run.

    Returns True when the selftest must not run. The caller exits 0 — a skip is not a failure —
    but the marked line is what stops `verify.py` from counting it as a pass. verify.py already
    treats a silent zero-exit as a failure; this closes the matching hole, a zero-exit that
    SPOKE while executing nothing.
    """
    for reason in reasons:
        if reason:
            print(f"{module} selftest: {PREREQ_ABSENT_MARK} {reason}")
            return True
    return False


def runnable(gaps: list[str], *reasons: str | None) -> bool:
    """Should this SECTION of a selftest run here? Records the reason when it must not.

    Gating a whole `--selftest` because one block of it needs the ledger would throw away the
    other few hundred assertions in the same function — running less to report green, which is
    the exact trade this project refuses. So the gate goes around the smallest block that needs
    the missing thing, `gaps` collects why, and `report_gaps` says so at the end.
    """
    for reason in reasons:
        if reason:
            gaps.append(reason)
            return False
    return True


def report_gaps(module: str, gaps: list[str]) -> None:
    """Print the marked line naming every section that did not run, or nothing if all did."""
    for reason in dict.fromkeys(gaps):
        print(f"{module} selftest: {PREREQ_ABSENT_MARK} section skipped — {reason}")


def _selftest() -> None:
    # The exception must be a skip to every harness that will see it.
    assert issubclass(MissingPrerequisite, unittest.SkipTest)
    try:
        require(None, None)
    except MissingPrerequisite:  # pragma: no cover
        raise AssertionError("require() must not raise when nothing is absent")
    try:
        require(None, "the named thing is missing", "a later reason")
    except MissingPrerequisite as exc:
        assert str(exc) == "the named thing is missing", exc
    else:
        raise AssertionError("require() must raise on the first named absence")

    # EVERY detector must answer with a reason that NAMES the thing, or with None. A detector
    # returning a bare True/False is the failure this module exists to prevent: a skip with no
    # reason is indistinguishable from a pass.
    detectors = [
        ("ledger_rows_absent", lambda: ledger_rows_absent("definitely-not-a-capability")),
        (
            "ledger_version_lineage_absent",
            lambda: ledger_version_lineage_absent("definitely-not-a-capability"),
        ),
        (
            "ledger_invocation_history_absent",
            lambda: ledger_invocation_history_absent("definitely-not-a-capability"),
        ),
        ("ledger_legacy_rows_absent", ledger_legacy_rows_absent),
        ("skill_resource_absent", skill_resource_absent),
        ("codex_profile_binary_absent", codex_profile_binary_absent),
        ("agent_cli_absent", lambda: agent_cli_absent("codex")),
        ("credential_file_absent", lambda: credential_file_absent("vibe")),
        ("seat_has_no_free_signal", seat_has_no_free_signal),
    ]
    for name, fn in detectors:
        got = fn()
        assert got is None or (isinstance(got, str) and len(got) > 20), (name, got)
    # The three that were handed a capability that cannot exist MUST report absence, or the
    # detector is not detecting anything.
    for name, fn in detectors[:3]:
        got = fn()
        assert got and "definitely-not-a-capability" in got, (name, got)

    # A skipped selftest must SPEAK, and its line must carry the shared mark verify.py greps
    # for. A skip that prints nothing is a silent zero-exit by another name.
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert selftest_skipped("mod", None, "thing X is absent") is True
        assert selftest_skipped("mod", None, None) is False
    text = buf.getvalue()
    assert PREREQ_ABSENT_MARK in text and "thing X is absent" in text, text
    assert text.count(PREREQ_ABSENT_MARK) == 1, text

    print(
        "env_prereq.py selftest: OK (skip-is-a-skip, every detector names the missing thing, "
        "marked selftest skip speaks)"
    )


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    # Default: report what this machine can and cannot check. Useful on a new box.
    checks = {
        "reference skill resource": skill_resource_absent(),
        "exact-profile Codex binary": codex_profile_binary_absent(),
        "seat auth signal": seat_has_no_free_signal(),
        "vibe credential file": credential_file_absent("vibe"),
        "ledger legacy rows": ledger_legacy_rows_absent(),
    }
    for name, reason in checks.items():
        print(
            f"{'ABSENT ' if reason else 'present'} {name}" + (f"\n    {reason}" if reason else "")
        )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
