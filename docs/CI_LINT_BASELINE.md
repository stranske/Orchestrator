# Gate python-ci baseline — what is checked, what is deferred, and what would clear it

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
| `typecheck-mypy` | `mypy --exclude .workflows-lib .` | **604 errors in 89 of 189 files** | **0 per PR** | OFF |
| `coverage` | `pytest --cov` | **0 startup errors** | 0 | **ON, green** |
| `python 3.12` / `python 3.13` | `pytest -m 'not quarantine and not slow' -n auto --dist=loadgroup` | **0 failures** (393 passed, 9 skipped) | 0 | **ON, green** |

One OFF row remains, and it states both quantities on purpose. `604 errors` alone reads as
*be patient*; `604 errors, drainable 0 per PR` reads as what it is. (`coverage` was the second such
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

* **Blocking:** 601 errors in 89 of 189 files. Top codes: `arg-type` 149, `index` 111, `assignment`
  78, `union-attr` 43, `no-redef` 43, `attr-defined` 42, `operator` 37, `import-not-found` 25.
* **Drainable:** 0 per PR. There is no `mypy --fix`; every one is an annotation or logic change in a
  distinct module.
* **Drains by:** typed modules landing incrementally. Flip `typecheck` on the day the count reaches
  zero — **not** by adding a `disable_error_code` list. Fifteen codes cover 597 of the 601 and would
  produce a green job that checks essentially nothing, which is precisely the defect `verify.py`
  exists to stop.
* **`mypy.ini` is committed even though the check is off**, and for one reason: without it `mypy .`
  aborts with `Source file found twice under different module names: "_llm_client" and
  "scripts.langchain._llm_client" ... (errors prevented further checking)`. That single setup error
  is all a reader of the failed job would have seen; it masked the real number entirely. With
  `explicit_package_bases = True` the 601 above is a number anyone can regenerate rather than prose.
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
