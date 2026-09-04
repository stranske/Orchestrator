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
import re

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


def _mask_javascript_non_code(source: str) -> str:
    """Mask comments and strings while preserving offsets for structural checks."""
    masked = list(source)
    state = "code"
    quote = ""
    index = 0

    def mask(position: int) -> None:
        if source[position] not in "\r\n":
            masked[position] = " "

    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char in {"'", '"', "`"}:
                quote = char
                state = "string"
                mask(index)
            elif char == "/" and following == "/":
                state = "line-comment"
                mask(index)
                mask(index + 1)
                index += 1
            elif char == "/" and following == "*":
                state = "block-comment"
                mask(index)
                mask(index + 1)
                index += 1
        elif state == "string":
            mask(index)
            if char == "\\" and following:
                mask(index + 1)
                index += 1
            elif char == quote:
                state = "code"
        elif state == "line-comment":
            mask(index)
            if char in "\r\n":
                state = "code"
        else:
            mask(index)
            if char == "*" and following == "/":
                mask(index + 1)
                index += 1
                state = "code"
        index += 1
    return "".join(masked)


def _matching_delimiter_index(source: str, opening_index: int, opening: str, closing: str) -> int:
    """Return the matching delimiter for a small JavaScript expression or block."""
    depth = 0
    for index in range(opening_index, len(source)):
        if source[index] == opening:
            depth += 1
        elif source[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unclosed JavaScript delimiter")


def _matching_brace_index(source: str, opening_index: int) -> int:
    return _matching_delimiter_index(source, opening_index, "{", "}")


def _pooled_validation_has_safe_control_flow(source: str) -> bool:
    """Check the pooled implementation as ordered scopes, not unrelated markers."""
    code = _mask_javascript_non_code(source)
    try:
        normalizer_index = code.index("const configuredWorkflowIds = (declared) =>")
        normalizer_end = code.index("const errorMessage", normalizer_index)
        normalizer_arrow = code.index("=>", normalizer_index, normalizer_end) + 2
        array_guard_match = re.match(
            r"\s*Array\.isArray\(\s*declared\s*\)\s*\?",
            code[normalizer_arrow:normalizer_end],
        )
        if array_guard_match is None:
            return False
        array_check_index = normalizer_arrow + array_guard_match.start()
        string_filter_index = code.index("typeof workflow ===", array_check_index, normalizer_end)
        string_filter_match = re.match(
            r"typeof\s+workflow\s*===\s*(['\"])string\1",
            source[string_filter_index:normalizer_end],
        )
        if string_filter_match is None:
            return False
        trim_index = code.index(
            ".map((workflow) => workflow.trim())", string_filter_index, normalizer_end
        )
        nonempty_filter_index = code.index(".filter(Boolean)", trim_index, normalizer_end)
        empty_fallback_index = code.index(": []", nonempty_filter_index, normalizer_end)
        default_index = code.index("let workflowIds = DEFAULT_WORKFLOWS;")
        declared_index = code.index(
            "const declared = configuredWorkflowIds(cfg.source_workflows);", default_index
        )
        configured_branch_index = code.index("if (declared.length) {", declared_index)
        configured_branch_open = code.index("{", configured_branch_index)
        configured_branch_close = _matching_brace_index(code, configured_branch_open)
        replacement_index = code.index("workflowIds = declared;", configured_branch_open)
        selection_end = code.index("const retryHelperPath", configured_branch_close)
        if not configured_branch_open < replacement_index < configured_branch_close:
            return False

        loop_index = code.index("for (const wf of workflowIds)", selection_end)
        loop_open = code.index("{", loop_index)
        loop_close = _matching_brace_index(code, loop_open)
        empty_result_index = code.index("if (!runs.length)", loop_close)
        query_index = code.index("const found = await paginateWithRetry(", loop_open, loop_close)
        query_open = code.index("(", query_index)
        query_close = _matching_delimiter_index(code, query_open, "(", ")")
        try_matches = list(re.finditer(r"\btry\s*\{", code[loop_open + 1 : query_index]))
        if not try_matches:
            return False
        try_index = loop_open + 1 + try_matches[-1].start()
        try_open = code.index("{", try_index, query_index)
        try_close = _matching_brace_index(code, try_open)
        if not try_open < query_index < query_close < try_close:
            return False
        query_scope = code[query_open + 1 : query_close]
        workflow_id_properties = list(re.finditer(r"(?m)^[ \t]*workflow_id[ \t]*:", query_scope))
        workflow_id_match = re.search(
            r"(?m)^[ \t]*workflow_id:[ \t]*wf,[ \t]*$",
            query_scope,
        )
        if len(workflow_id_properties) != 1 or workflow_id_match is None:
            return False
        workflow_id_index = query_open + 1 + workflow_id_match.start()
        successful_match = re.search(
            r"\bconst\s+successfulForWorkflow\s*=\s*found\b",
            code[query_close:try_close],
        )
        if successful_match is None:
            return False
        successful_index = query_close + successful_match.start()
        pool_index = code.index(
            "runs = runs.concat(successfulForWorkflow);", successful_index, try_close
        )
        catch_match = re.match(r"\s*catch\s*\(\s*err\s*\)\s*\{", code[try_close + 1 : loop_close])
        if catch_match is None:
            return False
        catch_index = code.index(
            "catch", try_close + 1 + catch_match.start(), try_close + 1 + catch_match.end()
        )
        catch_open = try_close + catch_match.end()
        catch_close = _matching_brace_index(code, catch_open)
        unavailable_index = code.index("searched.push(", catch_open, catch_close)
        unavailable_open = code.index("(", unavailable_index)
        unavailable_close = _matching_delimiter_index(code, unavailable_open, "(", ")")
        if (
            "`${wf} (UNAVAILABLE: ${errorMessage(err)})`"
            not in source[unavailable_open + 1 : unavailable_close]
        ):
            return False
    except ValueError:
        return False

    if not (
        normalizer_index
        < array_check_index
        < string_filter_index
        < trim_index
        < nonempty_filter_index
        < empty_fallback_index
        < normalizer_end
        < default_index
        < declared_index
        < configured_branch_open
        < configured_branch_close
        < selection_end
        < loop_index
        < try_index
        < try_open
        < query_index
        < workflow_id_index
        < query_close
        < successful_index
        < pool_index
        < try_close
        < catch_index
        < unavailable_index
        < unavailable_close
        < catch_close
        < loop_close
        < empty_result_index
    ):
        return False
    catch_scope = code[catch_open:catch_close]
    return re.search(r"\b(?:break|return|throw)\b", catch_scope) is None


POOLED_CONTROL_FLOW_EXAMPLE = """
const configuredWorkflowIds = (declared) =>
  Array.isArray(declared)
    ? declared
        .filter((workflow) => typeof workflow === 'string')
        .map((workflow) => workflow.trim())
        .filter(Boolean)
    : [];
const errorMessage = (error) => String(error);
let workflowIds = DEFAULT_WORKFLOWS;
const declared = configuredWorkflowIds(cfg.source_workflows);
if (declared.length) {
  workflowIds = declared;
}
const retryHelperPath = './github-api-with-retry.js';
for (const wf of workflowIds) {
  try {
    const found = await paginateWithRetry(
      github,
      github.rest.actions.listWorkflowRuns,
      {
        workflow_id: wf,
      },
    );
    const successfulForWorkflow = found.filter(Boolean);
    runs = runs.concat(successfulForWorkflow);
  } catch (err) {
    searched.push(`${wf} (UNAVAILABLE: ${errorMessage(err)})`);
  }
}
if (!runs.length) {
  core.setFailed('no runs');
}
"""


def test_pooled_control_flow_example_is_accepted() -> None:
    assert _pooled_validation_has_safe_control_flow(POOLED_CONTROL_FLOW_EXAMPLE)


def test_pooled_control_flow_accepts_double_quoted_string_filter() -> None:
    equivalent = POOLED_CONTROL_FLOW_EXAMPLE.replace("'string'", '"string"', 1)
    assert _pooled_validation_has_safe_control_flow(equivalent)


@pytest.mark.parametrize("statement", ("break", "return", "throw(err)"))
def test_pooled_control_flow_rejects_early_exit_variants(statement: str) -> None:
    unsafe = POOLED_CONTROL_FLOW_EXAMPLE.replace(
        "searched.push(`${wf} (UNAVAILABLE: ${errorMessage(err)})`);",
        f"searched.push(`${{wf}} (UNAVAILABLE: ${{errorMessage(err)}})`);\n    {statement}",
    )
    assert not _pooled_validation_has_safe_control_flow(unsafe)


@pytest.mark.parametrize(
    "decoy",
    (
        "workflow_id: DEFAULT_WORKFLOW, // workflow_id: wf,",
        "workflow_id: DEFAULT_WORKFLOW,\n        /*\n        workflow_id: wf,\n        */",
    ),
)
def test_pooled_control_flow_rejects_commented_workflow_id_decoys(decoy: str) -> None:
    unsafe = POOLED_CONTROL_FLOW_EXAMPLE.replace("workflow_id: wf,", decoy)
    assert not _pooled_validation_has_safe_control_flow(unsafe)


@pytest.mark.parametrize(
    "duplicate",
    (
        "workflow_id: DEFAULT_WORKFLOW,\n        workflow_id: wf,",
        "workflow_id: wf,\n        workflow_id: DEFAULT_WORKFLOW,",
    ),
)
def test_pooled_control_flow_rejects_duplicate_workflow_id_properties(duplicate: str) -> None:
    unsafe = POOLED_CONTROL_FLOW_EXAMPLE.replace("workflow_id: wf,", duplicate, 1)
    assert not _pooled_validation_has_safe_control_flow(unsafe)


def test_pooled_control_flow_keeps_default_for_an_empty_configured_list() -> None:
    unsafe = POOLED_CONTROL_FLOW_EXAMPLE.replace("if (declared.length) {", "if (true) {", 1)
    assert not _pooled_validation_has_safe_control_flow(unsafe)


def test_pooled_control_flow_requires_a_positive_array_guard() -> None:
    unsafe = POOLED_CONTROL_FLOW_EXAMPLE.replace(
        "Array.isArray(declared)", "!Array.isArray(declared)", 1
    )
    assert not _pooled_validation_has_safe_control_flow(unsafe)


def test_pooled_control_flow_binds_unavailable_catch_to_the_pagination_try() -> None:
    unsafe = POOLED_CONTROL_FLOW_EXAMPLE.replace(
        "  } catch (err) {",
        "  } finally {\n    cleanup();\n  }\n  try {\n    observe();\n  } catch (err) {",
        1,
    )
    assert not _pooled_validation_has_safe_control_flow(unsafe)


def test_pooled_control_flow_rejects_commented_normalization_decoys() -> None:
    unsafe = POOLED_CONTROL_FLOW_EXAMPLE.replace(
        ".map((workflow) => workflow.trim())",
        ".map((workflow) => workflow) // .map((workflow) => workflow.trim())",
        1,
    )
    assert not _pooled_validation_has_safe_control_flow(unsafe)


def test_pooled_control_flow_requires_found_runs_to_join_the_pool() -> None:
    unsafe = POOLED_CONTROL_FLOW_EXAMPLE.replace(
        "runs = runs.concat(successfulForWorkflow);",
        "// runs = runs.concat(successfulForWorkflow);",
        1,
    )
    assert not _pooled_validation_has_safe_control_flow(unsafe)


def test_pooled_control_flow_rejects_a_found_identifier_prefix_decoy() -> None:
    unsafe = POOLED_CONTROL_FLOW_EXAMPLE.replace(
        "const successfulForWorkflow = found.filter(Boolean);",
        "const successfulForWorkflow = foundFallback.filter(Boolean);",
        1,
    )
    assert not _pooled_validation_has_safe_control_flow(unsafe)


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
