# Gate python-ci baseline — what is checked, what is deferred, and what would clear it

## Measured dependency closeout toolchain — 2026-10-22

<!-- measured-with: black=26.10.1 ruff=0.17.2 isort=9.0.2 docformatter=1.7.8 mypy=2.4.0 pytest=9.1.1 pytest-cov=7.1.0 pytest-xdist=3.8.0 coverage=7.17.0 -->

The complete selected nine-pin bundle was installed in an isolated Python 3.12.2
virtual environment and strictly measured against source
`c28b6c3ab24ab3db06b2834d3fc383bfac44bd91`.
Ruff reported zero findings, Black left 652 files unchanged, mypy checked 158
source files with no errors, and the collector's Coverage startup probe reported
zero errors. The collector reported no version or setup problems.

[Strict report](evidence/dev-tool-876-current-pins/report.json),
[actual commands and output](evidence/dev-tool-876-current-pins/commands.json),
and [source and tool provenance](evidence/dev-tool-876-current-pins/provenance.json)
retain the measurements. The startup probe does not establish full-suite or
combined-coverage acceptance; those remain required native CI checks. Earlier
measurements and all enforcement settings remain intact.

## Measured dependency PR #655 toolchain — 2026-10-18

<!-- measured-with: black=26.10.1 ruff=0.17.1 isort=9.0.2 docformatter=1.7.8 mypy=2.4.0 pytest=9.1.1 pytest-cov=7.1.0 pytest-xdist=3.8.0 coverage=7.16.2 -->

The exact proposed nine-pin bundle was installed in an isolated Python 3.12
virtual environment and strictly measured against source
`6f9fff13673bbc5c53bd76cca5ac2b0a49f32a41`. The existing collector reported:

| Check | Blocking | Drainable |
| --- | ---: | ---: |
| Ruff | 0 findings | 0 |
| Black | 0 files | 0 |
| mypy | 0 errors | 0 |
| Coverage startup | 0 errors | 0 |

[Report](evidence/dev-tool-655-current-pins/report.json),
[captured lint commands and output](evidence/dev-tool-655-current-pins/commands.json),
and [source/tool provenance](evidence/dev-tool-655-current-pins/provenance.json)
retain the actual measurement, installed versions and collector/pin hashes.
All captured lint commands exited zero. Coverage startup is the collector's
configuration probe; it does not establish full-suite, execution-mirror or
combined-coverage acceptance. Native PR CI retains those checks. No pins,
thresholds, baseline matcher or enforcement settings were changed by this
measurement. Previous dated measurements remain below.

## Measured unchanged repository toolchain — 2026-10-15

<!-- measured-with: ruff=0.16.10 black=26.10.0 mypy=2.4.0 pytest=9.1.1 isort=9.0.2 docformatter=1.7.8 pytest-cov=7.1.0 pytest-xdist=3.8.0 coverage=7.16.2 -->

The unchanged current bundle was also freshly installed and strictly measured
against `b0aa0da43c128b763099e42b70d4c3cfb99a2f30`. All nine actual versions,
commands and zero-blocker report are retained in
[evidence/issue-518-successor607/current-pins](evidence/issue-518-successor607/current-pins/).
This supplies a complete nine-pin current measurement without inventing values
for old four-pin historical markers. Unknown, incomplete and hybrid bundles
are rejected across all nine tools, including coverage startup dependencies.

## Measured pending Maint 52 successor toolchain — 2026-10-15

<!-- measured-with: ruff=0.17.0 black=26.10.1 mypy=2.4.0 pytest=9.1.1 isort=9.0.2 docformatter=1.7.8 pytest-cov=7.1.0 pytest-xdist=3.8.0 coverage=7.16.2 -->

Generated PR #607 at `c3f7ac876c23511f81ed2f728a11e982cf6e002a`
proposes a newer toolchain than the October 11 measurement. All nine exact
proposed packages were installed in an isolated Python 3.12.2 environment and
initially measured against source `3da6bb956cecbb4ff1eca70a4e40c88f8573a341`.
The corrected strict-nine collector was rerun at `65fb9b0bb846bec44160a0ba1dca0e22e6eeffbc`;
that exact source and script/pin hashes are bound by the final report/provenance.
The original baseline test fails against those authenticated proposed pins;
this actual measurement supplies the missing complete recorded toolchain.

| Check | Blocking | Drainable |
| --- | ---: | ---: |
| Ruff | 0 findings | 0 |
| Black | 0 files | 0 |
| mypy | 0 errors | 0 |
| Coverage startup | 0 errors | 0 |

[Retained evidence](evidence/issue-518-successor607/README.md) includes every
command and captured output, strict report, installed versions, source head,
and script/pin digests. Existing historical measurements remain below.
Coverage startup does not establish full-suite, execution-mirror or combined
coverage acceptance. The integrated 3,470 collection floor (current main3,469 plus one executed strict-version regression), capability/skip controls,
complete-toolchain matching and enforcement policy remain unchanged. No
repository pins or generated branch were changed for this preparation.

## Measured pending Maint 52 toolchain — 2026-10-11

<!-- measured-with: ruff=0.16.10 black=26.10.0 mypy=2.4.0 pytest=9.1.1 -->

The proposed toolchain from generated PR #517, exact head
`c93fe39be4fa497475fdb915ffe10631b4cb186e`, was actually run against source
`7728066fa679ff8941a696d4b52c5b7e460f4607` using Python 3.12.2. The complete
strict measurement, commands/output, installed versions, pin-file digest and
measurement-script digest are retained in
[`report.json`](evidence/issue-518-toolchain-baseline/report.json).
The authenticated proposed pin bytes are in the adjacent `proposed-pins.env`.
No generated branch or shared/repo pin was edited for this measurement.

| Check | Blocking | Drainable | Measured command |
| --- | ---: | ---: | --- |
| Ruff | 0 findings | 0 | `ruff check --extend-exclude .workflows-lib --output-format concise .` |
| Black | 0 files | 0 | `black --check --line-length 100 --exclude '(\.venv|\.workflows-lib|node_modules)' .` |
| mypy | 0 errors | 0 | `mypy --config-file pyproject.toml --exclude .workflows-lib src` |
| Coverage startup | 0 errors | 0 | `pytest --cov --cov-config=pyproject.toml --collect-only -q -p no:cacheprovider` |

This execution also exposed and repaired a real counter defect: mypy's
`annotation-unchecked` informational notes were counted as blocking errors when
the tool reported success. Only actual error diagnostics now count; a deliberate
old-counter regression failed and the fixed regression passed. The table's
coverage row proves startup only, not the combined-coverage or execution-mirror
verdicts. Those remain separate required delivery checks; the 2,672 collection
floor and capability/skip guards are unchanged.

The version-coupling regression requires one **complete** recorded toolchain to
match the installed pin bundle. It supports both the unchanged current pins and
the measured pending pins, so the preparation can merge before Maint 52 applies
the new pins. Unknown, incomplete and hybrid bundles still fail. This is not
permission to invent a marker without running the measured commands.

## Historical baseline — current pre-sync pin bundle

The older numbers and rationale below are retained as dated history, not fresh
counts for the pending toolchain. Rerun the script before quoting them.

<!-- measured-with: ruff=0.16.4 black=26.5.1 mypy=2.3.1 pytest=9.1.1 -->

**Do not trust the numbers below because they are written down.** Regenerate them:

```bash
python3 scripts/ci_lint_baseline.py
```

That script runs the *exact* commands `stranske/Workflows/.github/workflows/reusable-10-ci-python.yml`
runs, and refuses to print anything if the installed tool versions differ from
`.github/workflows/autofix-versions.env` — because these counts are version-specific. This document
is the record of one measurement, on 2026-08-23; the script is the authority.

## The thing that was actually broken

Until 2026-08-23 every Python pull request failed six checks — `lint-ruff`, `lint-format`,
`typecheck-mypy`, `python 3.12`, `python 3.13` and `summary`. It is natural to read that as a repo
drowning in lint debt. It was not. All five upstream jobs died at the **same shared step**, before
any tool ran:

```
Error: /home/runner/work/Orchestrator/Orchestrator/.github/workflows/autofix-versions.env
       is required; refusing to install unpinned tooling.
```

`reusable-10-ci-python.yml` will not install tooling without that pin file, and requires an exact
`==` pin for `pytest` and `pytest-xdist` **unconditionally**. Two consequences worth stating plainly:

* **Ruff, Black and mypy had never executed on this repository, not once.** Any statement about its
  lint debt before this date was inferred, not measured.
* **Turning the toggles off could not have fixed it.** The tests job needs the same pin file, so
  `lint: false, format_check: false, typecheck: false` would have left `python 3.12`, `python 3.13`
  and `summary` red for the identical reason.

Why the file was missing is documented upstream, in the two places that disagree:

| Upstream file | What it says |
| --- | --- |
| `stranske/Workflows/.github/sync-manifest.yml:33` | `pr-00-gate.yml` is synced to consumers (`sync_mode: create_only`). |
| `stranske/Workflows/.github/sync-manifest.yml:967` | `.github/workflows/autofix-versions.env` is on the **excluded** list — *"Repo-specific CI version pins; consumers copy or override per docs/ci/WORKFLOWS.md. Intentionally not synced"*. |
| `stranske/Workflows/docs/ci/WORKFLOWS.md:83` | The consumer procedure: *"Tool/test pins come from `.github/workflows/autofix-versions.env`; consumers can copy that file"*. |

So the sync delivered a Gate that hard-requires a file the sync deliberately never delivers, and the
repo never performed the documented copy step. The fix is that copy.

A second blocker was hidden behind the first, and only became visible once the pins landed:
**coverage**. It was drained upstream on 2026-08-23 and is now ON — see below.

## Current state

Measured 2026-08-23 with the pinned versions, after the drain described below.

| check | Gate command | blocking | drainable | state |
| --- | --- | --- | --- | --- |
| `lint-ruff` | `ruff check --extend-exclude .workflows-lib .` | **0 findings** | 0 | **ON, green** |
| `lint-format` | `black --check --line-length 100 --exclude '(\.venv\|\.workflows-lib\|node_modules)' .` | **0 files** | 0 | **ON, green** |
| `typecheck-mypy` | `mypy --exclude .workflows-lib .` | **608 errors in 89 of 189 files** | **0 per PR** | OFF |
| `coverage` | `pytest --cov` | **0 startup errors** | 0 | **ON, green** |
| `python 3.12` / `python 3.13` | `pytest -m 'not quarantine and not slow' -n auto --dist=loadgroup` | **0 failures** (393 passed, 9 skipped) | 0 | **ON, green** |

One OFF row remains, and it states both quantities on purpose. An error count alone reads as
*be patient*; the same count with `drainable 0 per PR` beside it reads as what it is. (`coverage` was the second such
row until 2026-08-23; the entry below records how it drained rather than deleting the history.) The authoritative
copy of each of those annotations lives beside the toggle itself, in the `Compute Python CI toggles`
step of `.github/workflows/pr-00-gate.yml` — one place, so the call site and the `summary` job's
coverage branch cannot drift apart.

## The loop this closes, and why the drain landed here

Two CI surfaces run Ruff and Black on this repo, and with **no config file present they resolved
"no config" differently**:

| surface | what it applies | on today's `main` |
| --- | --- | --- |
| Gate (`reusable-10-ci-python.yml:1155`) | `ruff check --select E4,E7,E9,F` — the pre-0.16 default, pinned deliberately for config-less consumers | **37** findings |
| Autofix (`reusable-18-autofix.yml:523-527`) | `ruff check --select I --fix`, then bare `ruff check --fix` — Ruff **0.16's own default**, far wider — then `black --line-length 100` | **733** findings |

So Autofix rewrote the tree to satisfy a rule set the Gate never checked, on **every** Gate failure,
and the Gate stayed red regardless because it was dying at the install step. That produced four
`chore(autofix): formatting/lint` commits on PR #42 and one on PR #51, each around 143 files and
+20,000/−10,000 lines. Reverting one just got it re-pushed. Two windows that could never agree.

Before #42 merged those figures were **79** and **915** — which is where the "915 findings, no PR
can drain it" reading came from. The gap is the defect, not its size on any given day.

`ruff.toml` collapses the two into one window: Ruff reads it from either surface, so what Autofix
fixes is exactly what the Gate checks. Bringing the tree *to* that canon is what makes the loop
structurally dead rather than merely dormant — **the Autofix sweep is now a provable no-op**,
verified by replaying its three commands verbatim and observing zero changes:

```
ruff check --select I --fix --exit-zero .   ->  All checks passed!
ruff check --fix --exit-zero .             ->  All checks passed!
black --line-length 100 .                  ->  196 files left unchanged
```

Because #42's Autofix commits reached `main` first, most of that reformat is already there: the drain
in this change is **39 Ruff findings and 2 files**, not the 141-and-126 it would have been a day
earlier. Deferring the format check instead would have left the loop armed — the next Gate failure of
any kind would spawn another mass-reformat commit — and `.autofix-exclude`, the only repo-owned
lever, cannot express "do not format this repo" (its patterns filter *directories*, and the repo root
is always a target). Editing `autofix.yml` would not survive either: it carries no `sync_mode`, so
the next template sync overwrites it.

## Deferred Ruff rules, with counts and drains

`ruff.toml` selects `E4`, `E7`, `E9`, `F`, `I`. Everything else is deferred deliberately:

| rule(s) | blocking | drainable | why deferred / what drains it |
| --- | --- | --- | --- |
| `E501` line-too-long | **1068 lines** still over 100 columns *after* `black -l 100` | **0 mechanically** | They are long strings, URLs and comment prose. No formatter can break them; only rewriting load-bearing text would, and this repo's aligned multi-line strings carry ledger prose that is the point. Selecting `E501` would be selecting a rule with no drain. |
| Ruff 0.16's default set beyond `E4/E7/E9/F` — `B`, `BLE`, `EXE`, `ISC`, `S`, `SIM`, `TRY`, `UP`, `FURB`, `PL`, `RUF`, `DTZ`, `PIE` | **742 findings** (the selected set sits at 0) | 9 auto-fixable, the rest hand edits | Each is a real code change, not a reformat: the bulk are `BLE001` blind-except, `EXE001` shebang and `S110` try-except-pass. Widening the selection is a deliberate act — measure, drain to zero in the same change, *then* add the code to `ruff.toml`. |
| `RUF100` unused-`noqa` | **72** | 72 | Not selected on purpose. Those 72 `# noqa` comments name rules the narrow set does not check; deleting them would have to be undone the moment the selection widens. |

The line length is **100, not Black's default 88**, and that number is load-bearing: the Gate's
format job and Autofix both hardcode `black --line-length 100` on their command lines. At 88, Black
rewrites **184 of 196 files** here; at 100 it rewrites **2** — and reformatting at 88 is exactly what
generated the churn commits above. `test_ci_gate_config.py` asserts `ruff.toml`'s `line-length` still
equals that 100, so the three cannot drift.

Three `E402` findings are exempted per-site with `# noqa: E402` and a reason, not by an `ignore` in
`ruff.toml`: each import follows a deliberate `sys.path.insert(...)` that is what makes it resolvable,
so hoisting it would break the module. The rule stays on for every other file.

## Deferred: mypy

* **Blocking:** 608 errors in 89 of 189 files, across 19 distinct codes. Top codes: `arg-type` 151,
  `index` 111, `assignment` 79, `attr-defined` 46, `no-redef` 43, `union-attr` 43, `operator` 37,
  `import-not-found` 25.
* **This figure drifts, and nothing couples it to a measurement.** It read 601, then 604, then 607,
  then 608 during 2026-08-23 alone, as typed code landed; the three places that recorded it
  disagreed with each other and with reality at the same time. `test_ci_gate_config.py` asserts only
  that a disabled toggle *states* a blocking and a drainable quantity, never that the number is
  right — it cannot, without making the suite depend on an installed mypy. Re-run
  `python3 scripts/ci_lint_baseline.py` before quoting it.
* **Drainable:** 0 per PR. There is no `mypy --fix`; every one is an annotation or logic change in a
  distinct module.
* **Drains by:** typed modules landing incrementally. Flip `typecheck` on the day the count reaches
  zero — **not** by adding a `disable_error_code` list. The 15 commonest of the 19 codes cover 603 of
  the 608, so such a list would produce a green job that checks essentially nothing, which is
  precisely the defect `verify.py` exists to stop.
* **`mypy.ini` is committed even though the check is off**, and for one reason: without it `mypy .`
  aborts with `Source file found twice under different module names: "_llm_client" and
  "scripts.langchain._llm_client" ... (errors prevented further checking)`. That single setup error
  is all a reader of the failed job would have seen; it masked the real number entirely. With
  `explicit_package_bases = True` the count above is one anyone can regenerate rather than prose.
  It must be `mypy.ini` and not `pyproject.toml` or `setup.cfg` — see the next section.

## Drained 2026-08-23: coverage

Kept rather than deleted, because the shape of this one is the lesson: a gate whose only exit lay
outside the repo that hosted it.

* **What blocked it:** 1 startup error, and it was never about this repo's tests. With
  `coverage: true` the reusable workflow appended `--cov-config=pyproject.toml` unconditionally.
  This repo has no `pyproject.toml`, so pytest died before collecting anything:
  `coverage.exceptions.ConfigError: Couldn't read 'pyproject.toml' as a config file` — both
  runtimes, every test.
* **Why drainable was 0 *from here*:** the two upstream assumptions were mutually exclusive.
  Adding a `pyproject.toml` to satisfy the coverage flag made the *same* workflow append
  `-e '.[app,dev]'` to its install, and 129 flat root modules with no build backend cannot be
  installed that way. No change inside this repository could satisfy both — which is precisely why
  the annotation named an **upstream** drain instead of asking anyone here to be patient.
* **How it drained:** `stranske/Workflows#3202`, merged 2026-08-23. `--cov-config` is now passed
  only when the file exists (coverage otherwise uses its own `.coveragerc` / `setup.cfg` / `tox.ini`
  discovery), and the editable install is gated on real packaging metadata — `[project]`,
  `[build-system]` or `[tool.poetry]` — rather than on the filename. The `python ci` job pins that
  workflow `@main`, so this repo picked the fix up with no version bump.
* **Verified here, not assumed:** `pytest --cov --collect-only` now collects 402 tests in this tree
  and emits a coverage report, with no `ConfigError`. `scripts/ci_lint_baseline.py` no longer
  *asserts* this number — `measure_coverage()` runs that probe, so an upstream regression comes back
  as a `1` in the table above instead of as silence.
* **Still true:** this repo has no `pyproject.toml` and no `.coveragerc`, so coverage runs on its
  built-in defaults, and `coverage-min` stays `''` (informational only). `test_ci_gate_config.py`
  still fails if `setup.cfg` or `setup.py` appears, or a `pyproject.toml` that declares a
  distribution; a config-only `pyproject.toml` would no longer force an install.

## The pins and the numbers move together

`.github/workflows/autofix-versions.env` is a local copy of a fleet constant, and **nothing updates
it automatically**: Renovate's fleet preset excludes dev tools (they are owned by that file), and
`maint-52-sync-dev-versions.yml` propagates settled pins into a consumer `pyproject.toml`, which this
repo does not have. Re-copy from `stranske/Workflows` when the fleet bumps.

The safeguard is a coupling rather than a reminder: the `measured-with` comment at the top of this
file must equal the pins, and `test_ci_gate_config.py` fails if they diverge. Bumping a pin without
re-measuring therefore turns the suite red. That is deliberate — Ruff 0.16 widening its own default
from 37 findings to 733 is exactly the kind of silent event that would otherwise change what the Gate
measures behind a baseline nobody re-ran.
