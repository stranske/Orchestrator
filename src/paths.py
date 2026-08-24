#!/usr/bin/env python3
"""paths.py — where the modules live, and where the CHECKOUT root is. Two things, not one.

WHY THIS EXISTS. Until 2026-08-23 every module derived both answers from the same expression,
`Path(__file__).resolve().parent`, because they happened to be the same directory: the modules sat
at the repo root. They are not the same thing, and conflating them is what made a `src/` layout
look like a 126-file rewrite. Two distinct questions were being answered by one accident:

  * "where is my sibling MODULE?" — `capabilities.py`, `dispatcher.py`. Answer: MODULE_DIR.
  * "where is the CHECKOUT?" — `orchestrate.sh`, `.verify-floor.json`, `.coveragerc`, the sibling
    fleet repos two levels up. Answer: REPO_ROOT.

DETECTED, NOT HARDCODED, and that is the load-bearing part. `orch-sync-mirror.sh` copies modules
into the mirror that launchd actually runs, and it may copy them FLAT (module dir == repo root) or
under `src/`. A hardcoded `parent.parent` would be right in one tree and wrong in the other — the
exact failure `capability_activation_audit._fleet_roots` documents, where byte-identical code scored
37 of 37 in the canonical tree and 36 of 37 in the mirror. So the layout is *observed*: if the
module directory is named `src`, the checkout is its parent; otherwise the two coincide.

That makes this module a no-op on a flat tree, which is deliberate — it lands and is verified
BEFORE any file moves, so the move itself changes no behaviour here.

Deliberately dependency-free (pathlib only). Every module may import it without risking a cycle:
`capabilities` imports `feedback`, and `feedback`'s own selftest imports `env_prereq`, so anything
placed in those modules instead would close a loop for somebody.
"""

from __future__ import annotations

import os
from pathlib import Path

# The directory holding the orchestrator's modules — this file's own directory, by construction.
MODULE_DIR = Path(__file__).resolve().parent

# The checkout root: the directory holding `orchestrate.sh`, `.verify-floor.json`, `.coveragerc`,
# `pyproject.toml` and the docs. Equal to MODULE_DIR on a flat tree; its parent under `src/`.
REPO_ROOT = (
    MODULE_DIR.parent if MODULE_DIR.name == "src" else MODULE_DIR
)  # == checkout_root(MODULE_DIR)

# The directory holding the SIBLING FLEET repos (`Workflows`, `Counter_Risk`, ...). One level above
# the checkout, and never above the module dir — that distinction is the whole point of this file.
# `$ORCH_FLEET_ROOT` still wins where a caller consults it; this is only the derived default.
FLEET_ROOT = REPO_ROOT.parent


# Where the test suite lives. Named here because ONE module legitimately reaches into it —
# `capability_admission` reads the recurrence-fixture roster from `test_capability_set_coverage` —
# and an implicit `sys.path` accident is how that dependency would rot silently.
TESTS_DIR = REPO_ROOT / "tests"


def checkout_root(module_dir: Path) -> Path:
    """Apply the rule to an ARBITRARY module dir, not just this file's.

    The constants above are the common case, but a caller that resolves paths relative to its OWN
    module directory needs the rule applied to that — and its tests patch that directory to build
    synthetic trees. Reading a module-level constant would make those tests un-patchable and, worse,
    would silently ignore the tree the caller thinks it is looking at. One rule, applied wherever
    asked; that is what stops the module dir and the checkout root drifting apart again.
    """
    module_dir = Path(module_dir)
    return module_dir.parent if module_dir.name == "src" else module_dir


def fleet_root(module_dir: Path) -> Path:
    """Where the SIBLING FLEET repos sit, relative to an arbitrary module dir. One above the
    CHECKOUT — never one above the module dir, which is the same thing only on a flat tree."""
    return checkout_root(module_dir).parent


def orchestrate_sh() -> Path:
    """The tick driver, which lives at the CHECKOUT root rather than beside the modules.

    A function rather than a constant because several callers want to know whether it exists, and
    a constant would invite `if ORCHESTRATE:` — always true for a Path, existing or not.
    """
    return REPO_ROOT / "orchestrate.sh"


def _selftest() -> None:
    import tempfile

    # FLAT tree: the two roots coincide, so this module is inert before the move.
    assert MODULE_DIR == Path(__file__).resolve().parent
    if MODULE_DIR.name != "src":
        assert REPO_ROOT == MODULE_DIR, (REPO_ROOT, MODULE_DIR)

    # The detection itself, exercised on both shapes rather than on whichever one we are in —
    # the mirror may be flat while the checkout is not, and one run can only be in one of them.
    resolve = checkout_root  # the REAL rule, not a copy of it — a copy could pass while it drifts

    with tempfile.TemporaryDirectory(prefix="paths-selftest-") as td:
        root = Path(td)
        (root / "src").mkdir()
        assert resolve(root / "src") == root, "src/ layout must resolve the checkout to its parent"
        assert resolve(root) == root, "a flat tree must resolve the checkout to itself"
        # A directory that merely CONTAINS a src/ is not itself src/ — the name is the signal.
        assert resolve(root / "lib") == root / "lib"

    # The fleet root is one above the CHECKOUT, never one above the module dir. Under `src/` those
    # differ by a level, and getting it wrong is what put the mirror one capability short.
    assert FLEET_ROOT == REPO_ROOT.parent == fleet_root(MODULE_DIR)
    assert checkout_root(MODULE_DIR) == REPO_ROOT, "constants must come from the rule"
    assert orchestrate_sh().parent == REPO_ROOT, orchestrate_sh()
    # $ORCH_FLEET_ROOT is a caller's override, not this module's business; assert we do not read it.
    assert "ORCH_FLEET_ROOT" not in os.environ or FLEET_ROOT == REPO_ROOT.parent

    print(
        f"paths.py selftest: OK (module dir vs checkout root distinguished, layout DETECTED not "
        f"hardcoded for both flat and src/ shapes, fleet root anchored to the checkout; "
        f"here MODULE_DIR={MODULE_DIR.name!r} REPO_ROOT={REPO_ROOT.name!r})"
    )


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    print(f"MODULE_DIR = {MODULE_DIR}")
    print(f"REPO_ROOT  = {REPO_ROOT}")
    print(f"FLEET_ROOT = {FLEET_ROOT}")
    print(f"orchestrate.sh = {orchestrate_sh()} (exists: {orchestrate_sh().is_file()})")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
