"""The coverage baseline and the workflow that feeds it, checked against each other.

Two repos in this fleet were invisible to Maint Coverage Guard on 2026-08-30 and neither was
misconfigured in any way a reader could see. The guard needs a payload artifact AND a trend
artifact on a run of the workflow a repo declares; a config naming a workflow that publishes only
one of them produces the same silence as a repo measuring nothing at all. Counter_Risk is in
exactly that state today — both payloads, no trend.

So the pairing is asserted here rather than discovered from a guard that has been failing for
weeks: what the config NAMES must be a workflow that exists, and that workflow must publish BOTH
artifacts under the names the guard resolves.
"""

from __future__ import annotations

import json

import pytest

import env_prereq
import paths

REPO = paths.REPO_ROOT
BASELINE = REPO / "config/coverage-baseline.json"
MAINT_COVERAGE_GUARD = REPO / ".github/workflows/maint-coverage-guard.yml"

# The names Maint Coverage Guard resolves. `gate-coverage` is its preferred exact payload name;
# `gate-coverage-trend` is required outright, and a run carrying only the payload is skipped.
PAYLOAD_ARTIFACT = "gate-coverage"
TREND_ARTIFACT = "gate-coverage-trend"


def _matching_brace_index(source: str, opening_index: int) -> int:
    """Return the closing brace for a small JavaScript control-flow block."""
    depth = 0
    for index in range(opening_index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unclosed JavaScript block")


def _pooled_validation_has_safe_control_flow(source: str) -> bool:
    """Check the pooled implementation as ordered scopes, not unrelated markers."""
    try:
        default_index = source.index("let workflowIds = DEFAULT_WORKFLOWS;")
        declared_index = source.index(
            "const declared = configuredWorkflowIds(cfg.source_workflows);", default_index
        )
        configured_branch_index = source.index("if (declared.length) {", declared_index)
        configured_branch_open = source.index("{", configured_branch_index)
        configured_branch_close = _matching_brace_index(source, configured_branch_open)
        replacement_index = source.index("workflowIds = declared;", configured_branch_open)
        selection_end = source.index("const retryHelperPath", configured_branch_close)
        if not configured_branch_open < replacement_index < configured_branch_close:
            return False

        loop_index = source.index("for (const wf of workflowIds)", selection_end)
        loop_open = source.index("{", loop_index)
        loop_close = _matching_brace_index(source, loop_open)
        empty_result_index = source.index("if (!runs.length)", loop_close)
        query_index = source.index("const found = await paginateWithRetry(", loop_open, loop_close)
        catch_index = source.index("} catch (err) {", query_index, loop_close)
        catch_open = source.index("{", catch_index)
        catch_close = _matching_brace_index(source, catch_open)
        unavailable_index = source.index(
            "searched.push(`${wf} (UNAVAILABLE: ${errorMessage(err)})`);",
            catch_open,
            catch_close,
        )
    except ValueError:
        return False

    if not (
        default_index
        < declared_index
        < configured_branch_open
        < configured_branch_close
        < selection_end
        < loop_index
        < query_index
        < catch_index
        < unavailable_index
        < catch_close
        < loop_close
        < empty_result_index
    ):
        return False
    catch_scope = source[catch_open:catch_close]
    return all(token not in catch_scope for token in ("break;", "return;", "throw "))


@pytest.fixture(scope="module")
def baseline() -> dict:
    # A missing baseline is a SHAPE, not a wiring defect: the exec mirror carries modules plus a
    # named data-file list, and this file rides that list — but a mirror synced before the list
    # gained it must SKIP with the file named, never ERROR five tests into unreadability. The
    # 2026-08-30 mirror run did exactly that (5 ERRORs), which is how this guard got here.
    env_prereq.require(env_prereq.repo_files_absent("config/coverage-baseline.json"))
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_the_baseline_exists_and_is_a_number(baseline):
    """Without a baseline the guard has nothing to compare against and reports a no-op."""
    assert isinstance(baseline["line"], (int, float))
    assert 0 < baseline["line"] <= 100
    assert isinstance(baseline["warn_drop"], (int, float))


def test_the_baseline_is_below_the_measured_figure_so_it_can_fail(baseline):
    """A baseline above reality is red on arrival, and a gate red on arrival gets switched off.

    80.64% is what `verify.py --coverage` measured on 2026-08-30, against CI's 80.3%. The
    baseline is deliberately under both. This asserts the DIRECTION rather than the number, so it
    does not go stale the moment coverage moves.
    """
    assert baseline["line"] < 80.3, (
        "the baseline must sit below the last measured combined figure, or the guard opens a "
        "breach issue on a repo that has not regressed"
    )


def test_the_declared_source_workflow_exists(baseline):
    # The workflow FILES live under .github/, which the exec mirror deliberately does not carry —
    # directory-shaped absences get a guard, single data files get copied (the standing rule).
    env_prereq.require(env_prereq.repo_files_absent(".github/workflows"))
    declared = baseline.get("source_workflows")
    assert declared, "source_workflows must name where this repo's coverage is measured"
    for rel in declared:
        assert (REPO / rel).is_file(), f"{rel} is named in the baseline config but does not exist"


def test_coverage_guard_discovery_uses_declared_source_workflows(baseline):
    """Discovery must follow config rather than silently querying the Gate."""
    env_prereq.require(env_prereq.repo_files_absent(".github/workflows"))
    source = MAINT_COVERAGE_GUARD.read_text(encoding="utf-8")

    legacy_discovery = all(
        marker in source
        for marker in (
            "const configuredWorkflowIds = baseline.source_workflows;",
            "workflowIds = configuredWorkflowIds.map((workflowId) => workflowId.trim());",
            "workflowIds.map((workflowId)",
            "workflow_id: workflowId,",
        )
    )
    pooled_discovery = all(
        marker in source
        for marker in (
            "const declared = configuredWorkflowIds(cfg.source_workflows);",
            "workflowIds = declared;",
            "for (const wf of workflowIds)",
            "workflow_id: wf,",
        )
    )
    assert (
        legacy_discovery or pooled_discovery
    ), "coverage discovery must query every workflow declared by the repo-specific baseline"
    assert baseline["source_workflows"] != [".github/workflows/pr-00-gate.yml"]


def test_coverage_guard_normalizes_and_validates_declared_source_workflows(baseline):
    """Configured paths must be normalized and invalid sources must be visible.

    The legacy guard validates every path locally before replacing the Gate fallback. The pooled
    guard trims each configured value, keeps the fallback when the list is empty, and records an
    unavailable workflow while continuing to probe the other declared sources.
    """
    env_prereq.require(env_prereq.repo_files_absent(".github/workflows"))
    source = MAINT_COVERAGE_GUARD.read_text(encoding="utf-8")

    legacy_validation = all(
        marker in source
        for marker in (
            "workflowId.trim().startsWith('.github/workflows/')",
            "fs.existsSync(workflowId.trim())",
            "fs.statSync(workflowId.trim()).isFile()",
            "workflowIds = configuredWorkflowIds.map((workflowId) => workflowId.trim());",
        )
    )
    pooled_validation = all(
        marker in source
        for marker in (
            ".filter((workflow) => typeof workflow === 'string')",
            ".map((workflow) => workflow.trim())",
            ".filter(Boolean)",
        )
    )
    if pooled_validation:
        pooled_validation = _pooled_validation_has_safe_control_flow(source)
    assert (
        legacy_validation or pooled_validation
    ), "coverage discovery must normalize configured sources and expose unusable entries"
    assert all((REPO / path).is_file() for path in baseline["source_workflows"])


def test_the_declared_workflow_publishes_both_artifacts_the_guard_requires(baseline):
    """The defect this file exists for.

    A workflow publishing the payload but not the trend is skipped by the guard, and the message
    it produces says coverage is not being measured — which is the opposite of true. Nothing else
    in this repo would notice.
    """
    env_prereq.require(env_prereq.repo_files_absent(".github/workflows"))
    for rel in baseline["source_workflows"]:
        text = (REPO / rel).read_text(encoding="utf-8")
        for artifact in (PAYLOAD_ARTIFACT, TREND_ARTIFACT):
            assert f"name: {artifact}\n" in text, (
                f"{rel} is declared as this repo's coverage source but never publishes an "
                f"artifact named {artifact}; the guard skips such a run and reports it as "
                "coverage not being measured"
            )


def test_the_payload_comes_from_the_combined_run_not_the_gate(baseline):
    """Which number gets enforced is the whole point of pointing at ci.yml.

    The Gate runs pytest alone and reports 34.11%, because most of this codebase is exercised by
    `--selftest` subprocesses pytest-cov cannot see. Both figures are honestly produced; only one
    is about this codebase. If the declared workflow ever stopped running the combined
    measurement, the guard would silently start enforcing the blind spot.
    """
    env_prereq.require(env_prereq.repo_files_absent(".github/workflows"))
    for rel in baseline["source_workflows"]:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "--coverage" in text, f"{rel} must run the combined measurement"
        assert "coverage.json" in text, f"{rel} must publish the combined report as coverage.json"


def test_the_baseline_is_not_shipped_to_other_repos():
    """Every repo maintains its own baseline; syncing one would impose this repo's number.

    Asserted because the file lives at a template-managed path, and the reason it is excluded is
    not visible from the path itself.
    """
    env_prereq.require(
        env_prereq.repo_files_absent(
            ".github/workflows", ".gitignore", "config/coverage-baseline.json"
        )
    )
    manifest = REPO / ".github/workflows"
    assert manifest.is_dir()
    assert BASELINE.is_file()
    assert "coverage-baseline" not in (REPO / ".gitignore").read_text(encoding="utf-8"), (
        "the baseline must be COMMITTED — a gitignored baseline is absent in CI, which the guard "
        "reports as an unset baseline and treats as a no-op"
    )
