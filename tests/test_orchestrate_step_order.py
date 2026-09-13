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
