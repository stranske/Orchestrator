"""The Gate workflow's fork handling, exercised by running its real github-script steps under Node.

A pull request from a fork gets a read-only workflow token, so both of the Gate's writes -- the
consolidated summary comment and the `Gate / gate` commit status -- are refused there after the
Gate has already decided. Three steps of `.github/workflows/pr-00-gate.yml` handle that:

* `Classify pull request origin` is the ONE definition of "from a fork" (a deleted fork counts),
  handed to both writers as `PR_FROM_FORK`, so the two cannot disagree about it;
* `Ensure consolidated summary comment` falls back to the job summary when a fork's comment write
  is refused, and stays loud otherwise;
* `Report Gate commit status` keeps the retry helper's `gate-commit-status` task, under which the
  helper swallows a permission refusal, warns with the refusing token's name and returns null: an expected refusal only for a fork. A same-repository refusal fails loudly instead of
  leaving a stale commit-status verdict. For a fork, where no status can ever be written, the step records
  the verdict in the job summary and fails a verdict other than `success`.

Each script is extracted from the workflow and run against the REAL `.github/scripts` helpers it
requires; only the GitHub client and `core` are stubs. The checks need the workflow, those helpers
and a Node runtime. They run in CI, in every checkout and, since #387 ships `.github/` there, in
the exec mirror. The `env_prereq` guards skip them, by name, only in a tree that lacks one of the
three -- so a skip in the mirror now means the mirror is incomplete, never that it is expected.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

import env_prereq
import paths

WORKFLOW = ".github/workflows/pr-00-gate.yml"
RETRY_HELPER = ".github/scripts/github-api-with-retry.js"
COMMENT_HELPER = ".github/scripts/comment-dedupe.js"
ORIGIN_STEP = "Classify pull request origin"
COMMENT_STEP = "Ensure consolidated summary comment"
STATUS_STEP = "Report Gate commit status"

FORK = {"fromFork": "true", "head": "outside-contributor/Orchestrator"}
DELETED_FORK = {"fromFork": "true", "head": "deleted source repository"}
SAME_REPO = {"fromFork": "false", "head": "stranske/Orchestrator"}
REFUSED = {"status": 403, "message": "Resource not accessible by integration"}
# github-api-with-retry.js counts this 404 as a permission refusal exactly like the 403.
REFUSED_404 = {"status": 404, "message": "Resource not accessible by integration"}
RATE_LIMITED = {"status": 403, "message": "API rate limit exceeded for installation"}
RATE_LIMIT_ROUTES = {
    "message": RATE_LIMITED,
    "429": {"status": 429, "message": "x"},
    "header": {"status": 403, "message": "Forbidden", "headers": {"x-ratelimit-remaining": "0"}},
}


def _need_workflow() -> None:
    env_prereq.require(env_prereq.repo_files_absent(WORKFLOW))


def _need_harness() -> None:
    env_prereq.require(
        env_prereq.repo_files_absent(WORKFLOW, RETRY_HELPER, COMMENT_HELPER),
        env_prereq.node_absent(),
    )


def _workflow_lines() -> list[str]:
    """Read production by default, or an exported baseline for the refusal controls.

    GATE_TEST_WORKFLOW lets the same assertions exercise an unchanged historical workflow
    without replacing the protected production file. An invalid export must fail, not silently
    fall back to production and make the negative control appear to pass.
    """
    workflow = Path(os.environ.get("GATE_TEST_WORKFLOW", str(paths.REPO_ROOT / WORKFLOW)))
    return workflow.read_text(encoding="utf-8").splitlines()


def _step_lines(step_name: str) -> list[str]:
    """One step's lines, from its `- name:` up to the next step at the same indent."""
    lines = _workflow_lines()
    starts = [i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}"]
    assert len(starts) == 1, f"{WORKFLOW} defines {step_name!r} {len(starts)} times"
    start = starts[0]
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines):
        line = lines[end]
        depth = len(line) - len(line.lstrip())
        if line.strip() and depth <= indent:
            break
        end += 1
    return lines[start:end]


def _step_script(step_name: str) -> str:
    """The step's `script: |` block, dedented. Standard library only: PyYAML is not a dependency."""
    block = _step_lines(step_name)
    marks = [i for i, line in enumerate(block) if line.strip() == "script: |"]
    assert len(marks) == 1, f"{step_name!r} has {len(marks)} script blocks"
    return textwrap.dedent("\n".join(block[marks[0] + 1 :])).rstrip() + "\n"


def _run(tmp: Path, runner: str, step_name: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Run a harness under Node from a scratch directory with a minimal environment.

    Both are deliberate. On CI (`GITHUB_ACTIONS=true`) the retry helper records each rate-limit
    incident to `artifacts/rate-limit-incidents.ndjson` relative to the WORKING DIRECTORY, so a
    harness started in the checkout writes into it; and an inherited token would let the helpers
    build a real client. Results go to a file, not stdout, which a helper may also print to.
    """
    (tmp / "step.js").write_text(_step_script(step_name), encoding="utf-8")
    (tmp / "runner.js").write_text(runner, encoding="utf-8")
    (tmp / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    completed = subprocess.run(
        ["node", "runner.js", str(paths.REPO_ROOT), "step.js", "cases.json", "out.json"],
        cwd=tmp,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return dict(json.loads((tmp / "out.json").read_text(encoding="utf-8")))


HARNESS_PRELUDE = textwrap.dedent(
    """
    const fs = require('fs');
    const path = require('path');
    const vm = require('vm');
    const [repoRoot, stepPath, casesPath, outPath] = process.argv.slice(2);
    const src = fs.readFileSync(stepPath, 'utf8');
    const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));

    function makeError(spec) {
      if (!spec) return null;
      const error = new Error(spec.message);
      error.status = spec.status;
      error.response = {
        status: spec.status,
        headers: spec.headers || {},
        data: { message: spec.message },
      };
      return error;
    }

    function makeCore(record) {
      const summary = {
        addHeading() { return summary; },
        addRaw(text) { record.summaryRaw.push(String(text)); return summary; },
        async write() { record.summaryWrites += 1; },
      };
      return {
        setFailed: (message) => record.failures.push(String(message)),
        setOutput: (name, value) => { record.outputs[name] = String(value); },
        warning: (message) => record.warnings.push(String(message)),
        info() {}, debug() {}, notice() {}, error() {},
        summary,
      };
    }

    function newRecord() {
      return { failures: [], warnings: [], summaryRaw: [], summaryWrites: 0, outputs: {}, threw: null };
    }

    async function runScript(sandbox, record) {
      vm.createContext(sandbox);
      try {
        await vm.runInContext('(async () => {\\n' + src + '\\n})()', sandbox);
      } catch (error) {
        record.threw = { status: error.status === undefined ? null : error.status, message: String(error.message) };
      }
      return record;
    }

    async function main(runCase) {
      const outcomes = {};
      for (const spec of cases) outcomes[spec.name] = await runCase(spec);
      fs.writeFileSync(outPath, JSON.stringify(outcomes));
    }
"""
).strip()


STATUS_RUNNER = HARNESS_PRELUDE + textwrap.dedent(
    """

    const retryHelper = require(path.join(repoRoot, '.github/scripts/github-api-with-retry.js'));

    main(async (spec) => {
      const record = newRecord();
      record.statusRequests = [];
      const error = makeError(spec.error);
      const github = {
        rest: {
          repos: {
            createCommitStatus: async (request) => {
              record.statusRequests.push(request);
              if (error) throw error;
              return { status: 201, data: {} };
            },
          },
        },
      };
      return runScript({
        process: {
          env: {
            STATE: spec.state,
            DESCRIPTION: 'all checks passed',
            TARGET_URL: 'https://example.invalid/run',
            PR_FROM_FORK: spec.fromFork,
            PR_HEAD: spec.head,
          },
        },
        console: { log() {} },
        require: (id) => {
          if (id === './.github/scripts/github-api-with-retry.js') return retryHelper;
          throw new Error(`unexpected require: ${id}`);
        },
        core: makeCore(record),
        context: {
          repo: { owner: 'stranske', repo: 'Orchestrator' },
          sha: 'basesha',
          payload: { pull_request: { head: { sha: spec.sha || 'headsha' } } },
        },
        github,
      }, record);
    });
"""
)


COMMENT_RUNNER = HARNESS_PRELUDE + textwrap.dedent(
    """

    const dedupe = require(path.join(repoRoot, '.github/scripts/comment-dedupe.js'));
    fs.writeFileSync('gate-summary.md', 'GATE SUMMARY BODY\\n<!-- gate-summary: pr=1 -->\\n');

    main(async (spec) => {
      const record = newRecord();
      record.created = 0;
      const error = makeError(spec.error);
      const listComments = async () => ({ data: [] });
      // `__testMock` is the marker the rate-limit wrapper honours, so the REAL comment-dedupe.js
      // drives this stub client directly.
      const github = {
        __testMock: true,
        rest: {
          issues: {
            listComments,
            createComment: async () => {
              record.created += 1;
              if (error) throw error;
              return { status: 201, data: { id: 1 } };
            },
          },
        },
        paginate: async (method) => {
          if (method === listComments) return [];
          throw new Error('unexpected paginate');
        },
      };
      return runScript({
        process: { env: { PR_FROM_FORK: spec.fromFork, PR_HEAD: spec.head } },
        console: { log() {} },
        require: (id) => {
          if (id === 'path') return path;
          if (id === 'fs') return fs;
          if (id === './.github/scripts/comment-dedupe.js') return dedupe;
          throw new Error(`unexpected require: ${id}`);
        },
        core: makeCore(record),
        context: { repo: { owner: 'stranske', repo: 'Orchestrator' }, payload: { pull_request: { number: 1 } } },
        github,
      }, record);
    });
"""
)


ORIGIN_RUNNER = HARNESS_PRELUDE + textwrap.dedent(
    """

    main(async (spec) => {
      const record = newRecord();
      return runScript({
        console: { log() {} },
        core: makeCore(record),
        context: { payload: spec.payload },
      }, record);
    });
"""
)


STATUS_CASES: list[dict[str, Any]] = [
    *(
        {"name": f"{origin}_{error['status']}_{state}", **head, "state": state, "error": error}
        for origin, head in (("fork", FORK), ("deleted_fork", DELETED_FORK))
        for error in (REFUSED, REFUSED_404)
        for state in ("success", "failure", "error", "pending")
    ),
    *(
        {"name": f"same_repo_{state}", **SAME_REPO, "state": state, "error": REFUSED}
        for state in ("success", "failure", "error", "pending")
    ),
    *(
        {"name": f"same_repo_404_{state}", **SAME_REPO, "state": state, "error": REFUSED_404}
        for state in ("success", "failure", "error", "pending")
    ),
    *(
        {
            "name": f"same_repo_{error['status']}_{label}",
            **SAME_REPO,
            "state": raw_state,
            "error": error,
        }
        for error in (REFUSED, REFUSED_404)
        for label, raw_state in (("empty", ""), ("invalid", "unknown-verdict"))
    ),
    *(
        {
            "name": f"rate_limit_{origin}_{route}_{state}",
            **head,
            "state": state,
            "error": error,
        }
        for origin, head in (
            ("fork", FORK),
            ("deleted_fork", DELETED_FORK),
            ("same_repo", SAME_REPO),
        )
        for route, error in RATE_LIMIT_ROUTES.items()
        for state in ("success", "failure", "error", "pending")
    ),
    {
        "name": "server_error",
        **FORK,
        "state": "success",
        "error": {"status": 500, "message": "Internal error"},
    },
    {
        "name": "not_found",
        **SAME_REPO,
        "state": "success",
        "error": {"status": 404, "message": "Not Found"},
    },
    {
        # A 403 the helper does not count as a rate limit was not retried as one, so the step must
        # not treat it as one either: the two share the helper's definition.
        "name": "bare_retry_after",
        **FORK,
        "state": "success",
        "error": {"status": 403, "message": "Forbidden", "headers": {"retry-after": "60"}},
    },
    {"name": "written", **FORK, "state": "success", "error": None},
    {"name": "same_repo_written", **SAME_REPO, "state": "success", "error": None},
]

# A new PR head must appear in both the refused request and the failure diagnostic. Keep the
# original head controls as well: a hardcoded SHA must not satisfy either side of this check.
STATUS_CASES.extend(
    {**case, "name": f"{case['name']}_new_head", "sha": "updated-headsha"}
    for case in list(STATUS_CASES)
    if case["fromFork"] == "false" and case["error"] in (REFUSED, REFUSED_404)
)

COMMENT_CASES: list[dict[str, Any]] = [
    {"name": "fork", **FORK, "error": REFUSED},
    {"name": "deleted_fork", **DELETED_FORK, "error": REFUSED},
    {"name": "same_repo", **SAME_REPO, "error": REFUSED},
    {"name": "rate_limit", **FORK, "error": RATE_LIMITED},
    {"name": "server_error", **FORK, "error": {"status": 500, "message": "Internal error"}},
    {"name": "written", **FORK, "error": None},
]

BASE = {"repo": {"full_name": "stranske/Orchestrator"}}
ORIGIN_CASES: list[dict[str, Any]] = [
    {
        "name": "fork",
        "payload": {"pull_request": {"base": BASE, "head": {"repo": {"full_name": FORK["head"]}}}},
    },
    {"name": "deleted_fork", "payload": {"pull_request": {"base": BASE, "head": {"repo": None}}}},
    {
        "name": "deleted_fork_with_label",
        "payload": {
            "pull_request": {"base": BASE, "head": {"repo": None, "label": "gone:fix-branch"}}
        },
    },
    {
        "name": "same_repo",
        "payload": {
            "pull_request": {"base": BASE, "head": {"repo": {"full_name": SAME_REPO["head"]}}}
        },
    },
    {"name": "not_a_pull_request", "payload": {}},
]


@pytest.fixture(scope="module")
def status(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    _need_harness()
    return _run(tmp_path_factory.mktemp("gate-status"), STATUS_RUNNER, STATUS_STEP, STATUS_CASES)


@pytest.fixture(scope="module")
def comment(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    _need_harness()
    return _run(
        tmp_path_factory.mktemp("gate-comment"), COMMENT_RUNNER, COMMENT_STEP, COMMENT_CASES
    )


@pytest.fixture(scope="module")
def origin(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    _need_harness()
    return _run(tmp_path_factory.mktemp("gate-origin"), ORIGIN_RUNNER, ORIGIN_STEP, ORIGIN_CASES)


def _verdict_written_to_summary(case: dict[str, Any], state: str) -> bool:
    summary = " ".join(case["summaryRaw"])
    return case["summaryWrites"] == 1 and "headsha" in summary and f"**{state}**" in summary


def _assert_fork_refusal(case: dict[str, Any], state: str) -> None:
    """Both permission routes must publish the verdict once and preserve its failure floor."""
    assert case["threw"] is None, case
    expected_failures = (
        [] if state == "success" else [f"Gate verdict for headsha is '{state}': all checks passed"]
    )
    assert case["failures"] == expected_failures, case
    assert _verdict_written_to_summary(case, state), case
    assert "all checks passed" in " ".join(case["summaryRaw"]), case
    assert any("read-only" in w and f"'{state}'" in w for w in case["warnings"]), case
    assert any("blocked by permissions" in w for w in case["warnings"]), case
    assert not any("Rate limit" in w for w in case["warnings"]), case
    assert len(case["statusRequests"]) == 1, case
    request = case["statusRequests"][0]
    assert request["sha"] == "headsha" and request["state"] == state, case
    assert request["context"] == "Gate / gate", case


# ---- the commit-status writer ------------------------------------------------------------------


def test_a_fork_refusal_records_a_success_verdict_without_failing(status: dict) -> None:
    for origin in ("fork", "deleted_fork"):
        for code in (403, 404):
            _assert_fork_refusal(status[f"{origin}_{code}_success"], "success")


@pytest.mark.parametrize(
    "cases",
    [
        [(f"{origin}_403_failure", "failure") for origin in ("fork", "deleted_fork")],
        [(f"{origin}_403_error", "error") for origin in ("fork", "deleted_fork")],
        [(f"{origin}_403_pending", "pending") for origin in ("fork", "deleted_fork")],
        [(f"{origin}_404_failure", "failure") for origin in ("fork", "deleted_fork")],
        [
            (f"{origin}_404_{state}", state)
            for origin in ("fork", "deleted_fork")
            for state in ("error", "pending")
        ],
    ],
    ids=["403-failure", "403-error", "403-pending", "404-failure", "404-error-pending"],
)
def test_a_fork_refusal_fails_closed_for_any_other_verdict(
    status: dict, cases: list[tuple[str, str]]
) -> None:
    for name, state in cases:
        _assert_fork_refusal(status[name], state)


def test_a_deleted_fork_is_named_for_what_it_is(status: dict) -> None:
    for code in (403, 404):
        for state in ("success", "failure", "error", "pending"):
            case = status[f"deleted_fork_{code}_{state}"]
            assert any("deleted source repository" in w for w in case["warnings"]), case
            _assert_fork_refusal(case, state)


@pytest.mark.parametrize(
    "cases",
    [
        [("same_repo_success", "success")],
        [
            *[(f"same_repo_{state}", state) for state in ("failure", "error", "pending")],
            *[(f"same_repo_403_{label}", "pending") for label in ("empty", "invalid")],
        ],
        [
            *[
                (f"same_repo_404_{state}", state)
                for state in ("success", "failure", "error", "pending")
            ],
            *[(f"same_repo_404_{label}", "pending") for label in ("empty", "invalid")],
        ],
    ],
    ids=["same_repo_success-success", "same_repo_failure-failure", "same_repo_404-success"],
)
def test_a_same_repo_refusal_fails_loudly(status: dict, cases: list[tuple[str, str]]) -> None:
    """Refusals fail after verdict normalization, retaining the three baseline controls."""
    for name, state in cases:
        for suffix, sha in (("", "headsha"), ("_new_head", "updated-headsha")):
            case_name = name + suffix
            case = status[case_name]
            assert case["threw"] is not None, (case_name, case)
            assert case["threw"]["message"] == (
                f"Same-repository Gate status publication was refused for {sha}; "
                f"computed verdict '{state}' was not published."
            ), (case_name, case)
            assert len(case["statusRequests"]) == 1, (case_name, case)
            request = case["statusRequests"][0]
            assert request["sha"] == sha and request["state"] == state, (case_name, case)
            assert request["context"] == "Gate / gate", (case_name, case)
            assert case["summaryWrites"] == 0, (case_name, case)
            assert any("blocked by permissions" in w for w in case["warnings"]), (case_name, case)
            assert not any("read-only" in w for w in case["warnings"]), (case_name, case)


def _assert_rate_limited_post(status: dict, state: str) -> None:
    """Rate limits preserve the verdict floor independently of the PR's origin."""
    for origin in ("fork", "deleted_fork", "same_repo"):
        for route in RATE_LIMIT_ROUTES:
            name = f"rate_limit_{origin}_{route}_{state}"
            case = status[name]
            assert case["threw"] is None, (name, case)
            expected_failures = (
                []
                if state == "success"
                else [f"Gate verdict for headsha is '{state}': all checks passed"]
            )
            assert case["failures"] == expected_failures, (name, case)
            assert any("Rate limit" in w for w in case["warnings"]), (name, case)
            assert not any(
                "read-only" in w or "blocked by permissions" in w for w in case["warnings"]
            ), (
                name,
                case,
            )
            assert case["summaryWrites"] == 0 and case["summaryRaw"] == [], (name, case)
            assert len(case["statusRequests"]) >= 1, (name, case)
            for request in case["statusRequests"]:
                assert request["sha"] == "headsha" and request["state"] == state, (name, case)
                assert request["context"] == "Gate / gate", (name, case)


def test_a_rate_limited_post_keeps_its_own_path(status: dict) -> None:
    _assert_rate_limited_post(status, "success")


@pytest.mark.parametrize("state", ["failure", "error", "pending"])
def test_a_rate_limited_post_fails_closed_for_any_other_verdict(status: dict, state: str) -> None:
    _assert_rate_limited_post(status, state)


def test_other_post_errors_stay_loud(status: dict) -> None:
    expected = {"server_error": 500, "not_found": 404, "bare_retry_after": 403}
    for name, code in expected.items():
        case = status[name]
        assert case["threw"] and case["threw"]["status"] == code, (name, case)
        assert case["summaryWrites"] == 0, (name, case)


def test_a_written_status_is_silent(status: dict) -> None:
    for name in ("written", "same_repo_written"):
        case = status[name]
        assert case["threw"] is None and case["failures"] == [] and case["warnings"] == [], case
        assert case["summaryWrites"] == 0, case
        assert case["statusRequests"] == [
            {
                "owner": "stranske",
                "repo": "Orchestrator",
                "sha": "headsha",
                "state": "success",
                "context": "Gate / gate",
                "description": "all checks passed",
                "target_url": "https://example.invalid/run",
            }
        ], case


# ---- the summary-comment writer ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["fork", "deleted_fork"])
def test_a_fork_comment_refusal_falls_back_to_the_job_summary(comment: dict, name: str) -> None:
    case = comment[name]
    assert case["threw"] is None, case
    assert case["summaryWrites"] == 1 and "GATE SUMMARY BODY" in " ".join(case["summaryRaw"]), case
    assert any("read-only" in w for w in case["warnings"]), case


def test_a_same_repo_comment_refusal_stays_loud(comment: dict) -> None:
    case = comment["same_repo"]
    assert case["threw"] and case["threw"]["status"] == 403, case
    assert case["summaryWrites"] == 0, case


def test_a_rate_limited_comment_is_left_to_the_helper(comment: dict) -> None:
    """The real comment-dedupe.js logs and skips a rate limit, so it never reaches the fork path."""
    case = comment["rate_limit"]
    assert case["threw"] is None and case["summaryWrites"] == 0, case
    assert any("Rate limit" in w for w in case["warnings"]), case
    assert not any("read-only" in w for w in case["warnings"]), case


def test_other_comment_errors_stay_loud(comment: dict) -> None:
    case = comment["server_error"]
    assert case["threw"] and case["threw"]["status"] == 500, case


def test_a_written_comment_is_silent(comment: dict) -> None:
    case = comment["written"]
    assert case["threw"] is None and case["warnings"] == [] and case["summaryWrites"] == 0, case
    assert case["created"] == 1, case


# ---- the one fork definition -------------------------------------------------------------------


def test_the_origin_step_classifies_every_shape(origin: dict) -> None:
    expected = {
        "fork": ("true", FORK["head"]),
        "deleted_fork": ("true", "deleted source repository"),
        "deleted_fork_with_label": ("true", "gone:fix-branch"),
        "same_repo": ("false", SAME_REPO["head"]),
        "not_a_pull_request": ("false", ""),
    }
    for name, (from_fork, head) in expected.items():
        case = origin[name]
        assert case["threw"] is None, (name, case)
        assert case["outputs"] == {"from_fork": from_fork, "head": head}, (name, case)


def test_both_writers_read_the_one_fork_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fork-ness is decided once. Each writer must read it from the origin step, which must run
    first, and neither may look at the payload's head repository itself -- two definitions of one
    fact drift, and #352's two copies had already diverged when they merged."""
    _need_workflow()
    names = [line.strip()[len("- name: ") :] for line in _workflow_lines() if "- name: " in line]
    assert names.index(ORIGIN_STEP) < names.index(COMMENT_STEP) < names.index(STATUS_STEP)
    assert sum(line.strip() == "id: pr_" + "origin" for line in _workflow_lines()) == 1
    for step in (COMMENT_STEP, STATUS_STEP):
        block = "\n".join(_step_lines(step))
        assert block.count("steps.pr_" + "origin.outputs.from_fork") == 1, step
        script = _step_script(step)
        assert script.count("head?.repo") == 0 and script.count("head.repo") == 0, step

    # The baseline control changes only the workflow input, retaining the real helper and
    # assertions. Exercise selection here without changing this suite's 24-test collection.
    original = _workflow_lines()
    status_script = _step_script(STATUS_STEP)
    exported = tmp_path / "exported-gate.yml"
    exported.write_text("\n".join(original) + "\n# exported workflow control\n", encoding="utf-8")
    with monkeypatch.context() as selected:
        selected.setenv("GATE_TEST_WORKFLOW", str(exported))
        assert _workflow_lines() == original + ["# exported workflow control"]
        assert _step_script(STATUS_STEP) == status_script
        exported.unlink()
        with pytest.raises(FileNotFoundError):
            _workflow_lines()
    assert _workflow_lines() == original
