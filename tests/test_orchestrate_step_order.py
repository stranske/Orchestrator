"""orchestrate.sh calls its cadence helpers only AFTER defining them.

The rail-exercise step (PR #207) was inserted at the heartbeat-producers anchor, about a hundred
lines above ``_cadence_due()``. bash resolves a function at call time, so every hourly tick printed
``orchestrate.sh: line 213: _cadence_due: command not found`` to stderr and skipped the step: the
weekly cadence never ran once in nine days, ``_mark_fail`` never fired (the error is at the ``if``
line, before the step's own backoff and ALERT), and nothing counted the stderr lines — 82 in the
current log alone. Definition-before-use is a property of the FILE, so it is pinned here instead of
being rediscovered at the next tick. The second test pins that every step key handed to
``_cadence_due`` is one the cadence registry knows, because an unknown key ABORTs the step at runtime
with the same silence.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import paths

ORCHESTRATE = paths.REPO_ROOT / "orchestrate.sh"
HELPERS = ("_due", "_step_disabled", "_cadence_due", "_attempt_ok", "_mark_success", "_mark_fail")


def _lines() -> list[str]:
    return ORCHESTRATE.read_text(encoding="utf-8").splitlines()


def test_cadence_helpers_are_defined_before_first_use() -> None:
    lines = _lines()
    late: list[str] = []
    for helper in HELPERS:
        definition = next(
            (i for i, line in enumerate(lines, start=1) if re.match(rf"^{helper}\(\)", line)), None
        )
        assert definition is not None, f"{helper}() is not defined in orchestrate.sh"
        for i, line in enumerate(lines, start=1):
            code = line.split("#", 1)[0]
            if i == definition or not re.search(rf"(^|[^A-Za-z0-9_]){helper}(\s|$|;|\))", code):
                continue
            if i < definition:
                late.append(f"line {i} calls {helper} but it is defined at line {definition}")
    assert (
        not late
    ), "bash resolves functions at call time; these calls fail every tick:\n" + "\n".join(late)


def test_every_cadence_step_key_is_registered() -> None:
    keys = set(re.findall(r"_cadence_due ([a-z0-9-]+)", ORCHESTRATE.read_text(encoding="utf-8")))
    assert keys, "no _cadence_due call sites found — the regex or the file layout changed"
    shell = subprocess.run(
        [sys.executable, str(paths.MODULE_DIR / "cadence_registry.py"), "shell"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    registered = set(re.findall(r"^\s*([a-z0-9-]+)\)\s", shell, flags=re.M))
    unknown = sorted(keys - registered)
    assert not unknown, f"steps not known to cadence_registry (they ABORT at runtime): {unknown}"


def test_prologue_does_not_depend_on_bash_source_being_set() -> None:
    """bash 5.3 (Homebrew, 2026-09-12) made `${BASH_SOURCE[0]}` an unbound variable under `set -u` when the
    prologue is replayed with `bash -c`, which is how capability_recurrence_check evaluates it; the tick
    itself was unaffected (a script file sets BASH_SOURCE), so the red appeared only in the local verdict.
    The fragment is pinned once so a "simplification" back to the bare form is caught by name."""
    text = ORCHESTRATE.read_text(encoding="utf-8")
    needle = (
        'dirname "${BASH_SOURCE[0]:-$0}"'  # the code fragment, not the comment that explains it
    )
    assert (
        text.count(needle) == 1
    ), f"expected exactly one prologue use of {needle!r}, found {text.count(needle)}"
    assert (
        'dirname "${BASH_SOURCE[0]}"' not in text
    ), "the bare form breaks the `bash -c` replay under set -u"


def test_orchestrate_sh_is_executable() -> None:
    # launchd runs `/bin/bash -lc '<mirror>/orchestrate.sh --active'`, which needs the mode bit.
    # git carried the file as 100644 until 2026-09-14, so a `cp` from any checkout produced a
    # non-executable copy; an interrupted mirror sync left exactly that, and three ticks died
    # with `Permission denied`. The bit is in the index now (100755), so every checkout — and
    # the sync's copy of it — carries it, and this fails any tree where it is missing.
    assert os.access(ORCHESTRATE, os.X_OK), f"{ORCHESTRATE} is not executable"
