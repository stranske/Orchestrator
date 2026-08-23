"""The Gate's python-ci configuration must stay honest: no silent skips, no drifting constants.

Every check here reads a committed file. None needs Ruff, Black, mypy or a populated capability
ledger installed, so none of them skips on any machine — deliberately. `verify.py` bounds skipping
with a ceiling, and the cheapest way to respect that budget is to write checks that never need it.

What these tests defend, and why each one has a scar behind it:

1.  **The pin file exists and pins exactly.** Its absence is what actually failed every python-ci
    job on this repo until 2026-08-23 — all five jobs died at the shared install step with
    "refusing to install unpinned tooling", so Ruff, Black and mypy had never run here at all. It
    reads as lint debt and is not.
2.  **The line length is one number, not three.** The Gate's format job and Autofix both hardcode
    `black --line-length 100`; `ruff.toml` and `scripts/ci_lint_baseline.py` must agree with them.
    A mismatched pair of literals is this workspace's most-repeated defect shape.
3.  **`ruff.toml` keeps an explicit `select`.** That declaration is what makes both surfaces apply
    the SAME rule set. Lose it and the Gate silently reverts to `--select E4,E7,E9,F` while Autofix
    reverts to Ruff 0.16's much wider default — the disagreement that produced four 143-file
    `chore(autofix)` commits on PR #42.
4.  **A disabled check states its blocking AND drainable quantity.** "601 errors" reads as
    be-patient; "601 errors, drainable 0 per PR" reads as a deadlock. The pair is the diagnosis.
5.  **One literal per toggle.** The `with:` block and the `summary` job's coverage branch both read
    `needs.detect.outputs.*`, so a second hardcoded `false` would be a literal that can drift.
6.  **The recorded baseline moves with the pins.** The counts are version-specific, so bumping a
    pin without re-measuring must go red rather than quietly re-describing a toolchain nobody runs.
7.  **`mypy.ini` never silences an error code.** Fifteen `disable_error_code` entries would cover
    597 of the 601 findings and produce a green job that checks nothing.
8.  **A citation names a file that is there.** These configs are prose-heavy on purpose: each
    tells the next reader where to re-measure before touching a pin. The pin file shipped citing
    `docs/ci/LINT_BASELINE.md` while the real path was `docs/CI_LINT_BASELINE.md`, so the single
    pointer to the baseline led nowhere — and prose is what no other check here reads. Scoped to
    the two files this repo OWNS; `pr-00-gate.yml` is synced from upstream and its unresolved
    script references are properly guarded (`hashFiles(...) != ''` with a named skip), so
    including it would only add noise, and a test that cries wolf gets waived.

DELIBERATE BREAK -> REVERT, performed 2026-08-23 (see `test_one_line_length_constant`):
changing `ruff.toml`'s `line-length = 100` to `88` failed exactly that test with
"ruff.toml line-length is 88 but the Gate and Autofix both pass --line-length 100"; reverting
restored a byte-identical file and the test passed again. The assertion is load-bearing, not
decorative. Repeated 2026-08-23 for `test_every_cited_repo_path_resolves`: restoring the original
`docs/ci/LINT_BASELINE.md` citation failed it with
"autofix-versions.env paragraph at line 23 cites docs/ci/LINT_BASELINE.md"; reverting passed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import env_prereq

HERE = Path(__file__).resolve().parent
GATE = HERE / ".github" / "workflows" / "pr-00-gate.yml"
PINS = HERE / ".github" / "workflows" / "autofix-versions.env"
RUFF_TOML = HERE / "ruff.toml"
MYPY_INI = HERE / "mypy.ini"
BASELINE_DOC = HERE / "docs" / "CI_LINT_BASELINE.md"
BASELINE_SCRIPT = HERE / "scripts" / "ci_lint_baseline.py"

# Only the two files this repo owns and hand-maintains. `GATE` is synced from upstream, and every
# unresolved script path in it sits behind an explicit guard, so it is deliberately not scanned.
CITING_FILES = (PINS, RUFF_TOML)
# A repo-relative path to something committed here. Bare filenames are not matched: `ruff.toml`
# citing `test_ci_gate_config.py` is a name, not a path, and needs no directory to resolve.
CITED_REPO_PATH = re.compile(r"(?<![\w./-])((?:docs|scripts|tools)/[\w./-]+\.(?:md|py|json|toml))")
# Naming the upstream repo marks a path as belonging to THAT tree, so it must not be resolved
# against this checkout. Scoped per PARAGRAPH, not per line: the prose wraps, and
# `docs/ci/WORKFLOWS.md` is quoted three lines below the sentence that says whose doc it is.
UPSTREAM_REPO = "stranske/Workflows"

# The value `reusable-10-ci-python.yml` and `reusable-18-autofix.yml` both hardcode on their Black
# command lines. Named once here; every other copy in this repo is asserted against it.
GATE_BLACK_LINE_LENGTH = 100

# `require_exact_pin` in the reusable workflow demands these two whatever the toggles say, so their
# absence breaks the tests job even with every check turned off.
PINS_REQUIRED_ALWAYS = ("PYTEST_VERSION", "PYTEST_XDIST_VERSION")
# Needed by the checks that are currently ON.
PINS_REQUIRED_FOR_ENABLED_CHECKS = ("RUFF_VERSION", "BLACK_VERSION")
# Not required today, but carried so that flipping a toggle is a one-line change rather than a
# rediscovery of this whole failure mode.
PINS_EXPECTED_FOR_FUTURE_TOGGLES = (
    "MYPY_VERSION",
    "ISORT_VERSION",
    "DOCFORMATTER_VERSION",
    "PYTEST_COV_VERSION",
    "COVERAGE_VERSION",
)

EXACT_VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")


def require_checkout() -> None:
    """Skip, naming what is missing, when this tree is the exec mirror rather than a checkout.

    Note what is gated and what is NOT. The gate is the presence of the repository DIRECTORIES the
    mirror never receives; the assertions inside each test still cover whether the specific FILE is
    there. Gating on `autofix-versions.env` itself would have made
    `test_pin_file_exists_and_pins_exactly` unable to fail — a check whose clear path is blocked by
    the very thing it measures, which is the defect this whole change exists to fix, reproduced
    inside its own test. Measuring window: is this a checkout. Draining window: is the file present.
    """
    env_prereq.require(env_prereq.repo_files_absent(".github/workflows", "docs", "scripts"))


def read_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in PINS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pins[key.strip()] = value.strip()
    return pins


def toggles_script() -> str:
    """The `Compute Python CI toggles` heredoc — the single source for every python-ci toggle."""
    text = GATE.read_text(encoding="utf-8")
    match = re.search(r"python - <<'PY'\n(.*?)\n          PY\n", text, re.S)
    assert match, "the toggles heredoc moved; this test can no longer read the toggle source"
    return match.group(1)


def python_ci_with_block() -> str:
    text = GATE.read_text(encoding="utf-8")
    match = re.search(r"\n  python-ci:\n(.*?)\n  [a-z][a-z0-9-]*:\n", text, re.S)
    assert match, "the python-ci job moved; this test can no longer read its `with:` block"
    return match.group(1)


def comment_paragraphs(text: str) -> list[tuple[int, str]]:
    """Split prose into blank-line-separated blocks, each tagged with its first line number.

    A citation belongs to the paragraph that frames it, which is why the upstream marker is
    searched per block rather than per line.
    """
    blocks: list[tuple[int, str]] = []
    current: list[str] = []
    start = 1
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip() in ("#", ""):
            if current:
                blocks.append((start, "\n".join(current)))
                current = []
            start = lineno + 1
        else:
            if not current:
                start = lineno
            current.append(line)
    if current:
        blocks.append((start, "\n".join(current)))
    return blocks


def test_pin_file_exists_and_pins_exactly():
    require_checkout()
    assert PINS.is_file(), (
        f"{PINS.relative_to(HERE)} is missing. reusable-10-ci-python.yml exits 1 in its shared "
        "install step without it, so lint-ruff, lint-format, typecheck-mypy, python 3.12 and "
        "python 3.13 all fail BEFORE any tool runs. Turning the toggles off does not help: pytest "
        "and pytest-xdist pins are required unconditionally. Copy the file from "
        "stranske/Workflows/.github/workflows/autofix-versions.env "
        "(see stranske/Workflows docs/ci/WORKFLOWS.md line 83)."
    )
    pins = read_pins()
    for key in PINS_REQUIRED_ALWAYS + PINS_REQUIRED_FOR_ENABLED_CHECKS:
        assert key in pins, f"{key} missing from {PINS.name}; the install step requires it"
    for key in PINS_EXPECTED_FOR_FUTURE_TOGGLES:
        assert key in pins, (
            f"{key} missing from {PINS.name}. It is not needed while its check is off, but "
            "carrying it means flipping that toggle stays a one-line change."
        )
    for key, value in pins.items():
        assert EXACT_VERSION.match(value), (
            f"{key}={value!r} is not an exact version. The workflow's `require_exact_pin` rejects "
            "anything without `==`, and an unpinned tool would silently change what the Gate "
            "measures — Ruff 0.16 widened its own default from 37 findings here to 733."
        )


def test_one_line_length_constant():
    """`ruff.toml`, the baseline script and the two upstream workflows must all say 100."""
    require_checkout()
    config = tomllib.loads(RUFF_TOML.read_text(encoding="utf-8"))
    assert config.get("line-length") == GATE_BLACK_LINE_LENGTH, (
        f"ruff.toml line-length is {config.get('line-length')} but the Gate and Autofix both pass "
        f"--line-length {GATE_BLACK_LINE_LENGTH}. Reformatting at Black's default 88 rewrites 184 "
        "files here instead of 2, and fights this repo's aligned multi-line strings — that mismatch "
        "what produced the +20,562/-11,089 churn commits. Change all of them or none."
    )
    script = BASELINE_SCRIPT.read_text(encoding="utf-8")
    assert f"GATE_LINE_LENGTH = {GATE_BLACK_LINE_LENGTH}" in script, (
        f"{BASELINE_SCRIPT.name} no longer measures at {GATE_BLACK_LINE_LENGTH} columns, so its "
        "reported format debt would not be the debt the Gate reports."
    )


def test_ruff_config_declares_an_explicit_selection():
    """Without `select`, both CI surfaces silently fall back to DIFFERENT default rule sets."""
    require_checkout()
    config = tomllib.loads(RUFF_TOML.read_text(encoding="utf-8"))
    lint = config.get("lint", {})
    selection = lint.get("select", lint.get("extend-select", config.get("select")))
    assert selection, (
        "ruff.toml declares no `select`. reusable-10-ci-python.yml checks for exactly that key to "
        "decide whether a consumer owns its rule set; without it the Gate reverts to "
        "`--select E4,E7,E9,F` while Autofix's bare `ruff check --fix` reverts to Ruff 0.16's much "
        "wider default. The two then disagree again, and Autofix resumes rewriting the tree to "
        "satisfy rules the Gate never checks."
    )


def test_every_disabled_toggle_states_blocking_and_drainable():
    """A gate that cannot say what would clear it is already defective."""
    require_checkout()
    script = toggles_script()
    disabled = re.findall(r"^\s*([a-z_]+) = False\s*$", script, re.M)
    assert disabled, (
        "no toggle is forced off in the `Compute Python CI toggles` step. If every check is now on, "
        "delete this test's expectation along with the annotations it guards."
    )
    for name in disabled:
        # The annotation block for a toggle is the comment run immediately above its assignment.
        block = script.split(f"{name} = False")[0].rsplit("\n\n", 1)[-1]
        for field in ("blocking:", "drainable:", "drains by:"):
            assert field in block, (
                f"the `{name} = False` toggle does not state `{field}` in the comment above it. "
                "Both quantities belong in the same place: '601 errors' reads as be-patient, "
                "'601 errors, drainable 0 per PR' reads as the deadlock it is. See "
                f"{BASELINE_DOC.relative_to(HERE)}."
            )


def test_the_with_block_holds_no_second_toggle_literal():
    """One literal per toggle, so the call site cannot drift from the summary job's view."""
    require_checkout()
    block = python_ci_with_block()
    for name in ("lint", "format_check", "typecheck", "run-mypy", "coverage"):
        match = re.search(rf"^      {re.escape(name)}: (.+)$", block, re.M)
        assert match, f"the python-ci `with:` block no longer passes `{name}`"
        value = match.group(1).strip()
        assert value.startswith("${{") and "needs.detect.outputs" in value, (
            f"`{name}: {value}` hardcodes a value in the `with:` block. Every toggle must read the "
            "one computed in `detect`, because the `summary` job's coverage branch reads the same "
            "output — two literals for one decision is how the measuring window stops matching the "
            "draining window."
        )


def test_baseline_doc_records_every_disabled_check_with_both_quantities():
    require_checkout()
    assert BASELINE_DOC.is_file(), f"{BASELINE_DOC.relative_to(HERE)} is missing"
    doc = BASELINE_DOC.read_text(encoding="utf-8")
    for name in re.findall(r"^\s*([a-z_]+) = False\s*$", toggles_script(), re.M):
        assert name in doc, (
            f"`{name}` is disabled in the Gate but {BASELINE_DOC.name} does not mention it. The "
            "recorded baseline is where a reader finds the count and the drain."
        )
    for field in ("blocking", "drainable", "drains by"):
        assert field in doc.lower(), f"{BASELINE_DOC.name} never states `{field}`"


def test_baseline_was_measured_with_the_pinned_versions():
    """The counts are version-specific, so a pin bump without a re-measure must go red."""
    require_checkout()
    doc = BASELINE_DOC.read_text(encoding="utf-8")
    match = re.search(r"<!--\s*measured-with:\s*(.*?)\s*-->", doc)
    assert match, (
        f"{BASELINE_DOC.name} has no `<!-- measured-with: ... -->` line. Without it there is no way "
        "to tell whether its numbers describe the toolchain CI actually installs."
    )
    measured = dict(pair.split("=", 1) for pair in match.group(1).split())
    pins = read_pins()
    for tool, version in measured.items():
        pin_key = f"{tool.upper().replace('-', '_')}_VERSION"
        assert pin_key in pins, f"{BASELINE_DOC.name} cites {tool}, which {PINS.name} does not pin"
        assert pins[pin_key] == version, (
            f"{BASELINE_DOC.name} recorded its counts with {tool} {version}, but {PINS.name} now "
            f"pins {pins[pin_key]}. Re-run `python3 scripts/ci_lint_baseline.py` and update both, "
            "or the baseline describes a toolchain nobody runs."
        )


def test_mypy_config_silences_nothing():
    """An OFF check is honest. A green check that examines nothing is not."""
    require_checkout()
    assert MYPY_INI.is_file(), (
        "mypy.ini is missing. Without it `mypy .` aborts on 'Source file found twice under "
        "different module names' and the recorded 601 becomes unverifiable prose."
    )
    text = MYPY_INI.read_text(encoding="utf-8")
    for forbidden in ("disable_error_code", "ignore_errors", "follow_imports = skip"):
        assert forbidden not in text, (
            f"mypy.ini sets `{forbidden}`. Fifteen disabled codes would cover 597 of the 601 "
            "findings and make typecheck-mypy green while checking nothing — the exact defect "
            "verify.py exists to stop. Leave the check OFF and drain the findings instead."
        )


@pytest.mark.parametrize("name", ["pyproject.toml", "setup.cfg", "setup.py"])
def test_no_packaging_file_appears_without_making_this_repo_installable(name):
    """A packaging file here must mean a REAL installable package, not a config parking spot."""
    require_checkout()
    assert not (HERE / name).is_file(), (
        f"{name} exists at the repo root. reusable-10-ci-python.yml appends `-e '.[app,dev]'` to "
        "its install when it sees setup.cfg / setup.py, or a pyproject.toml that declares a "
        "distribution ([project], [build-system] or [tool.poetry] — stranske/Workflows#3202 made "
        "that gate metadata-based rather than filename-based). 129 flat root modules with no build "
        "backend cannot be installed that way, so the tests job would fail for both runtimes. That "
        "is why the Ruff and mypy configuration lives in ruff.toml and mypy.ini instead. Adding one "
        "is fine, but it has to be a REAL installable package with `app` and `dev` extras. Then "
        "delete this expectation.\n\n"
        "Note: since #3202 a pyproject.toml carrying ONLY tool configuration no longer triggers the "
        "install, so relaxing this for that case is a legitimate change — but make it deliberately, "
        "and keep setup.cfg / setup.py forbidden, because those still do."
    )


def test_every_cited_repo_path_resolves():
    """A pointer to a file that is not there is worse than no pointer: it reads as verified."""
    require_checkout()
    dangling = []
    for path in CITING_FILES:
        if not path.is_file():
            continue
        for start, block in comment_paragraphs(path.read_text(encoding="utf-8")):
            if UPSTREAM_REPO in block:
                continue
            for cited in CITED_REPO_PATH.findall(block):
                if not (HERE / cited).is_file():
                    dangling.append(f"{path.name} paragraph at line {start} cites {cited}")
    assert not dangling, (
        "these citations name files that do not exist in this checkout:\n  "
        + "\n  ".join(dangling)
        + f"\n\nFix the path, or name `{UPSTREAM_REPO}` in the same paragraph if it genuinely "
        "refers to that repo's tree. The pin file shipped citing `docs/ci/LINT_BASELINE.md` when "
        "the real path was `docs/CI_LINT_BASELINE.md`, so the one comment telling a reader where "
        "to re-measure before bumping a pin pointed at nothing."
    )
