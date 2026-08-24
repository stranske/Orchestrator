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
4.  **A disabled check states its blocking AND drainable quantity.** An error count alone reads
    as be-patient; the same count with "drainable 0 per PR" beside it reads as a deadlock. The
    pair is the diagnosis.
5.  **One literal per toggle.** The `with:` block and the `summary` job's coverage branch both read
    `needs.detect.outputs.*`, so a second hardcoded `false` would be a literal that can drift.
6.  **The recorded baseline moves with the pins.** The counts are version-specific, so bumping a
    pin without re-measuring must go red rather than quietly re-describing a toolchain nobody runs.
7.  **The mypy config never silences an error code.** It lives in `pyproject.toml` now; CI
    passes `--config-file pyproject.toml` whenever that file exists. Fifteen `disable_error_code` entries would cover
    603 of the 608 findings and produce a green job that checks nothing.
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

import json
import re
import tomllib

import pytest

import env_prereq

# Repo-root files, resolved through the shared rule rather than a local `parent.parent`:
# these tests live in `tests/` while the things they assert on live at the checkout root.
import paths

HERE = paths.REPO_ROOT
GATE = HERE / ".github" / "workflows" / "pr-00-gate.yml"
PINS = HERE / ".github" / "workflows" / "autofix-versions.env"
RUFF_TOML = HERE / "ruff.toml"
PYPROJECT = HERE / "pyproject.toml"  # mypy config moved here; CI reads only this when it exists
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


def test_every_bounded_or_disabled_toggle_states_blocking_and_drainable():
    """A gate that cannot say what would clear it is already defective.

    ALL FIVE TOGGLES ARE NOW ON. `typecheck` was the last one forced off, and it went on when the
    src/ move scoped the Gate to 99 modules and pyproject.toml's per-module override list made the
    remaining findings exempt BY NAME — so the check runs over the clean modules today instead of
    over nothing. That does not retire this expectation, it moves it: a toggle that is ON but
    BOUNDED owes exactly the same three fields as one that is off, because "on" over a scoped
    subset can hide as much as "off" if the scope is not stated. So the annotation requirement now
    binds on any toggle that is disabled OR whose comment describes a bound, and the test asserts
    at least one such toggle exists — otherwise it would pass vacuously the moment someone deleted
    every annotation.
    """
    require_checkout()
    script = toggles_script()
    disabled = re.findall(r"^\s*([a-z_]+) = False\s*$", script, re.M)
    # A toggle whose annotation block claims a bound is held to the same standard as a disabled one.
    bounded = [
        name
        for name in re.findall(r"^\s*([a-z_]+) = RUN_CORE\s*$", script, re.M)
        if "drainable:" in script.split(f"{name} = RUN_CORE")[0].rsplit("\n\n", 1)[-1]
    ]
    annotated = disabled + bounded
    assert annotated, (
        "no toggle in the `Compute Python CI toggles` step is either forced off or annotated with a "
        "bound. Every check being unconditionally on is a legitimate end state — but then the "
        "blocking/drainable annotations have been deleted, and this test is the only thing that "
        "required them, so re-read the step before deleting this expectation."
    )
    for name in annotated:
        marker = " = False" if name in disabled else " = RUN_CORE"
        # The annotation block for a toggle is the comment run immediately above its assignment.
        block = script.split(f"{name}{marker}")[0].rsplit("\n\n", 1)[-1]
        for field in ("blocking:", "drainable:", "drains by:"):
            assert field in block, (
                f"the `{name}{marker}` toggle does not state `{field}` in the comment above it. "
                "Both quantities belong in the same place: an error count alone reads as "
                "be-patient, the same count with 'drainable 0 per PR' beside it reads as the "
                "deadlock it is. See "
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


def test_mypy_config_silences_nothing_by_error_code():
    """An OFF check is honest. A green check that examines nothing is not. But SCOPE is not SILENCE.

    THE DISTINCTION THIS TEST NOW DRAWS, because the old version forbade both and the difference is
    the whole design:

      * `disable_error_code` / `follow_imports = skip` make the check blind to a CLASS of error
        across every module, permanently, with nothing to count and no mechanism that removes it.
        Fifteen codes would have covered 603 of 608 findings. Still forbidden.
      * per-module `ignore_errors` names the modules that are not clean yet. Every finding stays
        discoverable (`scripts/ci_lint_baseline.py`), deleting a name restores that module's errors
        immediately, and `.verify-floor.json`'s `mypy_exempt_max` FAILS if the list grows. That is
        scoping with a drain and a bound, which is what let `typecheck` go from OFF over everything
        to ON over the clean 35.

    So the list is permitted and its BOUND is asserted — because an exempt list nobody counts is
    the same amnesty by a different route.

    Parsed from the TOML, never grepped from the text: the previous version substring-matched the
    file and tripped on a COMMENT explaining why `disable_error_code` was rejected. A check that
    fails on prose about itself teaches people to weaken it.
    """
    require_checkout()
    assert PYPROJECT.is_file(), (
        "pyproject.toml is missing. Without its [tool.mypy] section `mypy` aborts on 'Source "
        "file found twice under different module names' and the recorded count becomes "
        "unverifiable prose."
    )
    import tomllib

    with PYPROJECT.open("rb") as fh:
        mypy_cfg = tomllib.load(fh).get("tool", {}).get("mypy", {})
    sections = [mypy_cfg] + list(mypy_cfg.get("overrides") or [])
    for section in sections:
        for forbidden in ("disable_error_code", "follow_imports"):
            assert forbidden not in section, (
                f"the mypy config sets `{forbidden}`, which makes the check blind to a CLASS of "
                "error across modules — nothing to count, and no mechanism that removes it. "
                "Fifteen disabled codes would cover 603 of the 608 findings and make "
                "typecheck-mypy green while checking nothing, the exact defect verify.py exists to "
                "stop. Scope by MODULE with a counted, ceilinged list instead."
            )
    # Top-level `ignore_errors` would exempt everything at once, which has no drain either.
    assert "ignore_errors" not in mypy_cfg, (
        "[tool.mypy] sets a top-level `ignore_errors`, which exempts every module in one keyword. "
        "Use a per-module override list, which can be counted and bounded."
    )
    exempt = [
        m
        for o in (mypy_cfg.get("overrides") or [])
        if o.get("ignore_errors")
        for m in (o.get("module") if isinstance(o.get("module"), list) else [o.get("module")])
    ]
    if exempt:
        floor = json.loads((HERE / ".verify-floor.json").read_text(encoding="utf-8"))
        limit = floor.get("mypy_exempt_max")
        assert limit is not None, (
            f"{len(exempt)} module(s) are exempt from mypy but `.verify-floor.json` records no "
            "`mypy_exempt_max`. An exemption list nobody counts can only grow — that is an amnesty, "
            "not a ratchet. Record the bound."
        )
        assert len(exempt) <= int(limit), (
            f"{len(exempt)} modules are exempt from mypy but the agreed maximum is {limit}. The "
            "list may only ever shrink: type a module and delete its line, or raise the ceiling "
            "deliberately and say which module and why."
        )


@pytest.mark.parametrize("name", ["setup.cfg", "setup.py"])
def test_no_filename_triggered_packaging_file_appears(name):
    """`setup.cfg` / `setup.py` still trigger the editable install BY FILENAME, so they stay out.

    `reusable-10-ci-python.yml` appends `-e '.[app,dev]'` to its install when it sees either of
    these, with no metadata check — unlike pyproject.toml, which stranske/Workflows#3202 made
    metadata-based. This repo has ~99 flat modules and no build backend, so that install fails and
    both runtime jobs go red before a single test runs.
    """
    require_checkout()
    assert not (HERE / name).is_file(), (
        f"{name} exists at the repo root. reusable-10-ci-python.yml appends `-e '.[app,dev]'` to "
        f"its install on the mere PRESENCE of {name} — no metadata check, unlike pyproject.toml. "
        "With no build backend the tests job fails for both runtimes. Either make this a real "
        "installable package with `app` and `dev` extras, or keep tool configuration in "
        "pyproject.toml, which is metadata-gated."
    )


def test_pyproject_carries_tool_config_only_and_not_a_distribution():
    """THE DELIBERATE RELAXATION this file's previous expectation asked for, and its replacement.

    Until stranske/Workflows#3202 the Gate appended `-e '.[app,dev]'` on the mere existence of a
    pyproject.toml, so this repo could not have one at all and the Ruff/mypy settings lived in
    `ruff.toml` + `mypy.ini`. #3202 made that gate metadata-based via
    `pyproject_declares_distribution()`, and the old expectation here explicitly invited relaxing
    it for the tool-config-only case — "but make it deliberately". This is that change.

    What still has to hold, and why it is asserted rather than trusted: the moment this file grows
    `[project]`, `[build-system]` or `[tool.poetry]`, the Gate WILL try to install the repo. That
    is correct for a real package and fatal for ~99 flat modules with no backend, so the boundary
    gets a test rather than a comment. Adding a genuine package is fine — declare the backend and
    the `app`/`dev` extras, verify the install, and then change this test on purpose.
    """
    require_checkout()
    assert PYPROJECT.is_file(), "pyproject.toml is where the pytest, coverage and mypy config lives"
    import tomllib

    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    declares = sorted(k for k in ("project", "build-system") if k in data)
    if "poetry" in data.get("tool", {}):
        declares.append("tool.poetry")
    assert not declares, (
        f"pyproject.toml now declares {declares}, which makes `pyproject_declares_distribution()` "
        "true upstream and adds `-e '.[app,dev]'` to the install on all five Python jobs. This "
        "repo has ~99 flat modules under src/ and no build backend, so that install fails and the "
        "jobs go red before any test runs. If a real package is intended, add the backend and the "
        "app/dev extras, prove `pip install -e '.[app,dev]'` succeeds, and update this test."
    )
    # And the tool sections that had to move here must actually BE here — a pyproject.toml that
    # exists but omits them silently overrides .coveragerc / mypy.ini with nothing.
    for section in ("pytest", "coverage", "mypy"):
        assert section in data.get("tool", {}), (
            f"[tool.{section}] is missing. CI passes --cov-config/--config-file pointing at THIS "
            f"file whenever it exists, so an absent section is not a default — it is a silently "
            f"dropped setting."
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
