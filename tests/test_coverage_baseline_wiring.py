"""The coverage baseline and the workflow that feeds it, checked against each other.

Two repos in this fleet were invisible to Maint Coverage Guard on 2026-08-30 and neither was
misconfigured in any way a reader could see. The guard needs a payload artifact AND a trend
artifact on a run of the workflow a repo declares; a config naming a workflow that publishes only
one of them produces the same silence as a repo measuring nothing at all. Counter_Risk is in
exactly that state today — both payloads, no trend.

So the pairing is asserted here rather than discovered from a guard that has been failing for
weeks: what the config NAMES must be a workflow that exists, and that workflow must publish BOTH
artifacts under the names the guard resolves. Discovery itself is exercised as JavaScript with
hermetic API and filesystem doubles so both the legacy and pooled implementations are judged by
their behavior rather than by source-code tokens.
"""

from __future__ import annotations

import json
import shutil
import subprocess

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
FALLBACK_WORKFLOW = ".github/workflows/pr-00-gate.yml"
DISCOVERY_STEP = "      - name: Locate latest Gate workflow run\n"
SCRIPT_MARKER = "          script: |\n"
DISCOVERY_END = "\nif (!runs.length) {"

NODE_DISCOVERY_HARNESS = r"""
const vm = require('node:vm');
const payload = JSON.parse(process.argv[1]);
const requests = [];
const warnings = [];
const infos = [];
const failures = [];
const rejections = [];
const helperLoads = [];
const callTrace = [];
const outputs = {};
const existingFiles = new Set(payload.existing);
const existingDirectories = new Set(payload.directories);
const unavailable = new Set(payload.unavailable);
const rejectionNonce = require('node:crypto').randomUUID();
const retryHelperPath = './.github/scripts/github-api-with-retry.js';
const mockFs = {
  readFileSync(path) {
    if (path !== 'config/coverage-baseline.json') {
      throw new Error(`unexpected read: ${path}`);
    }
    return JSON.stringify({source_workflows: payload.workflows});
  },
  existsSync(path) {
    if (path === retryHelperPath) return payload.useRetryHelper;
    return existingFiles.has(path) || existingDirectories.has(path);
  },
  statSync(path) {
    if (!existingFiles.has(path) && !existingDirectories.has(path)) {
      const error = new Error(`ENOENT: no such file or directory, stat '${path}'`);
      error.code = 'ENOENT';
      throw error;
    }
    return {isFile: () => existingFiles.has(path)};
  },
};
const context = {repo: {owner: 'stranske', repo: 'Orchestrator'}};
const core = {
  warning(message) { warnings.push(String(message)); },
  info(message) { infos.push(String(message)); },
  setFailed(message) { failures.push(String(message)); },
  setOutput(name, value) { outputs[name] = String(value); },
};
const github = {
  rest: {actions: {listWorkflowRuns: Symbol('listWorkflowRuns')}},
  async paginate(_method, params) {
    if (_method !== github.rest.actions.listWorkflowRuns) {
      throw new Error('unexpected pagination method');
    }
    callTrace.push({type: 'github.paginate', workflow: params.workflow_id});
    requests.push(params.workflow_id);
    if (unavailable.has(params.workflow_id)) {
      const error = new Error(`unavailable: ${params.workflow_id} (${rejectionNonce})`);
      rejections.push({workflow: params.workflow_id, error: error.message});
      throw error;
    }
    return [{
      id: `run-${params.workflow_id}`,
      conclusion: 'success',
      run_started_at: '2026-09-04T00:00:00Z',
    }];
  },
};
const localRequire = (name) => {
  if (name === 'fs') return mockFs;
  if (name === retryHelperPath && payload.useRetryHelper) {
    helperLoads.push(name);
    const retryHelpers = require(name);
    return {
      ...retryHelpers,
      paginateWithRetry(...args) {
        callTrace.push({type: 'paginateWithRetry', workflow: args[2]?.workflow_id});
        return retryHelpers.paginateWithRetry(...args);
      },
    };
  }
  throw new Error(`unexpected require: ${name}`);
};
const body = [
  '(async () => {',
  payload.source,
  "const exportedSearched = typeof searched === 'undefined' ? [] : searched;",
  'return {exportedSearched, runIds: runs.map((run) => run.id)};',
  '})();',
].join('\n');
vm.runInNewContext(
  body,
  {context, core, github, require: localRequire},
  {timeout: 5000},
)
  .then((result) => process.stdout.write(JSON.stringify({
    ...result, requests, warnings, infos, failures, rejections, helperLoads, callTrace, outputs,
  })))
  .catch((error) => {
    process.stderr.write(`${error.stack || error}\n`);
    process.exitCode = 1;
  });
"""


def _discovery_script(workflow_source: str) -> str:
    """Extract the executable prefix that selects and queries coverage workflows."""
    step_index = workflow_source.find(DISCOVERY_STEP)
    assert step_index >= 0, f"{MAINT_COVERAGE_GUARD}: missing marker {DISCOVERY_STEP!r}"
    marker_index = workflow_source.find(SCRIPT_MARKER, step_index)
    assert marker_index >= 0, f"{MAINT_COVERAGE_GUARD}: missing marker {SCRIPT_MARKER!r}"
    script_index = marker_index + len(SCRIPT_MARKER)
    script_lines: list[str] = []
    for line in workflow_source[script_index:].splitlines():
        if line.startswith("            "):
            script_lines.append(line[12:])
        elif not line.strip():
            script_lines.append("")
        else:
            break
    script = "\n".join(script_lines)
    end_index = script.find(DISCOVERY_END)
    assert end_index >= 0, f"{MAINT_COVERAGE_GUARD}: missing marker {DISCOVERY_END!r}"
    return script[:end_index]


def _run_discovery(
    workflow_source: str,
    workflows: list[str],
    *,
    existing: set[str],
    unavailable: set[str] | None = None,
    directories: set[str] | None = None,
    use_retry_helper: bool = False,
) -> dict:
    """Execute workflow discovery with no network, repository, or runner side effects."""
    node = shutil.which("node")
    if node is None:
        raise env_prereq.MissingPrerequisite(
            "node executable is required to exercise the embedded github-script discovery block"
        )
    payload = {
        "source": _discovery_script(workflow_source),
        "workflows": workflows,
        "existing": sorted(existing),
        "directories": sorted(directories or set()),
        "unavailable": sorted(unavailable or set()),
        "useRetryHelper": use_retry_helper,
    }
    completed = subprocess.run(
        [node, "-e", NODE_DISCOVERY_HARNESS, json.dumps(payload)],
        check=False,
        capture_output=True,
        cwd=REPO,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _assert_rejection_is_recorded(result: dict, workflow: str) -> None:
    """Require the actual API rejection to produce the workflow's unavailable record."""
    assert len(result["rejections"]) == 1
    rejected = result["rejections"][0]
    assert rejected["workflow"] == workflow
    recorded = [
        entry
        for entry in result["exportedSearched"]
        if entry.startswith(f"{workflow} (UNAVAILABLE:") and rejected["error"] in entry
    ]
    assert len(recorded) == 1


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


def test_coverage_guard_discovery_uses_every_normalized_declared_workflow(baseline):
    """Discovery must query every configured path after trimming it."""
    env_prereq.require(env_prereq.repo_files_absent(".github/workflows"))
    source = MAINT_COVERAGE_GUARD.read_text(encoding="utf-8")
    declared = ["  .github/workflows/ci.yml  ", " .github/workflows/pr-00-gate.yml "]
    expected = [path.strip() for path in declared]

    result = _run_discovery(source, declared, existing=set(expected), use_retry_helper=True)

    assert result["requests"] == expected
    assert result["runIds"] == [f"run-{path}" for path in expected]
    assert result["failures"] == []
    assert result["helperLoads"] == ["./.github/scripts/github-api-with-retry.js"]
    expected_trace = [
        event
        for path in expected
        for event in (
            {"type": "paginateWithRetry", "workflow": path},
            {"type": "github.paginate", "workflow": path},
        )
    ]
    assert result["callTrace"] == expected_trace
    assert baseline["source_workflows"] != [FALLBACK_WORKFLOW]


def test_coverage_guard_exposes_an_unusable_declared_source_and_keeps_working(baseline):
    """Both supported implementations must fail visibly and retain a usable query path.

    The legacy guard rejects a missing local workflow and deliberately falls back to Gate. The
    pooled guard queries every declared workflow, records an unavailable API target, and retains
    runs from the other sources. The behavior, rather than a version marker, selects the branch.
    """
    env_prereq.require(env_prereq.repo_files_absent(".github/workflows"))
    source = MAINT_COVERAGE_GUARD.read_text(encoding="utf-8")
    missing = ".github/workflows/missing.yml"
    usable = ".github/workflows/ci.yml"
    result = _run_discovery(
        source,
        [missing, usable],
        existing={usable},
        unavailable={missing},
    )

    if result["requests"] == [FALLBACK_WORKFLOW]:
        assert any("falling back" in warning.lower() for warning in result["warnings"])
        assert result["runIds"] == [f"run-{FALLBACK_WORKFLOW}"]
    else:
        assert result["requests"] == [missing, usable]
        assert result["runIds"] == [f"run-{usable}"]
        _assert_rejection_is_recorded(result, missing)
    assert result["failures"] == []
    assert all((REPO / path).is_file() for path in baseline["source_workflows"])


def test_coverage_guard_does_not_treat_an_existing_directory_as_a_workflow(baseline):
    """Legacy validation must reject directories; pooled discovery must expose API rejection."""
    env_prereq.require(env_prereq.repo_files_absent(".github/workflows"))
    source = MAINT_COVERAGE_GUARD.read_text(encoding="utf-8")
    missing = ".github/workflows/missing.yml"
    usable = ".github/workflows/ci.yml"
    branch_probe = _run_discovery(
        source,
        [missing, usable],
        existing={usable},
        unavailable={missing},
    )

    directory = ".github/workflows/subdir"
    result = _run_discovery(
        source,
        [directory],
        existing=set(),
        directories={directory},
        unavailable={directory},
    )

    if branch_probe["requests"] == [FALLBACK_WORKFLOW]:
        assert result["requests"] == [FALLBACK_WORKFLOW]
        assert result["runIds"] == [f"run-{FALLBACK_WORKFLOW}"]
        assert any("falling back" in warning.lower() for warning in result["warnings"])
    else:
        assert result["requests"] == [directory]
        assert result["runIds"] == []
        _assert_rejection_is_recorded(result, directory)
    assert result["failures"] == []


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
