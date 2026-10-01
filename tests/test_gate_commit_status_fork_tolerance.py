"""Exercise the Gate commit-status script against fork token failures."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-00-gate.yml"
STEP_NAME = "Report Gate commit status"
COMMENT_STEP_NAME = "Ensure consolidated summary comment"

RUNNER_JS = textwrap.dedent("""
    const fs = require('fs');
    const vm = require('vm');
    const src = fs.readFileSync(process.argv[2], 'utf8');
    const retryScript = require(process.argv[3]);

    function makeError(status, message, headers = {}, responseMessage = null) {
      const error = new Error(message);
      error.status = status;
      error.response = { headers, data: { message: responseMessage } };
      return error;
    }

    async function runCase({ headRepo, baseRepo, error, state }) {
      const failures = [];
      const statusRequests = [];
      const warnings = [];
      const summaryWrites = [];
      const summaryRaw = [];
      const githubStub = {
        rest: {
          repos: {
            createCommitStatus: async (request) => {
              statusRequests.push(request);
              if (error) throw error;
            },
          },
        },
      };
      const summaryStub = {
        addHeading() { return summaryStub; },
        addRaw(text) { summaryRaw.push(String(text)); return summaryStub; },
        async write() { summaryWrites.push('write'); },
      };
      const sandbox = {
        process: {
          env: {
            STATE: state,
            DESCRIPTION: 'all checks passed',
            TARGET_URL: 'https://example.invalid/run',
          },
        },
        console: { log() {} },
        require: (request) => {
          if (request === './.github/scripts/github-api-with-retry.js') {
            return retryScript;
          }
          throw new Error(`unexpected require: ${request}`);
        },
        core: {
          setFailed: (message) => failures.push(String(message)),
          warning: (message) => warnings.push(String(message)),
          info: () => {},
          error: () => {},
          summary: summaryStub,
        },
        context: {
          repo: { owner: 'stranske', repo: 'Orchestrator' },
          sha: 'basesha',
          payload: {
            pull_request: {
              head: {
                sha: 'headsha',
                repo: headRepo === null ? null : { full_name: headRepo },
              },
              base: { repo: { full_name: baseRepo } },
            },
          },
        },
        github: githubStub,
      };
      vm.createContext(sandbox);
      let threw = null;
      try {
        await vm.runInContext('(async () => {\\n' + src + '\\n})()', sandbox);
      } catch (error) {
        threw = {
          status: error.status === undefined ? null : error.status,
          message: String(error.message),
        };
      }
      return {
        failures,
        warnings,
        summaryWrites: summaryWrites.length,
        summaryRaw,
        statusRequests,
        threw,
      };
    }

    const FORK = {
      headRepo: 'outside-contributor/Orchestrator',
      baseRepo: 'stranske/Orchestrator',
    };
    const SAME = {
      headRepo: 'stranske/Orchestrator',
      baseRepo: 'stranske/Orchestrator',
    };

    (async () => {
      const outcomes = {
        fork_read_only: await runCase({
          ...FORK,
          state: 'success',
          error: makeError(403, 'Resource not accessible by integration'),
        }),
        fork_read_only_failure: await runCase({
          ...FORK,
          state: 'failure',
          error: makeError(403, 'Resource not accessible by integration'),
        }),
        fork_read_only_error: await runCase({
          ...FORK,
          state: 'error',
          error: makeError(403, 'Resource not accessible by integration'),
        }),
        fork_read_only_pending: await runCase({
          ...FORK,
          state: 'pending',
          error: makeError(403, 'Resource not accessible by integration'),
        }),
        deleted_fork_read_only: await runCase({
          headRepo: null,
          baseRepo: 'stranske/Orchestrator',
          state: 'success',
          error: makeError(403, 'Resource not accessible by integration'),
        }),
        same_repo_read_only: await runCase({
          ...SAME,
          state: 'success',
          error: makeError(403, 'Resource not accessible by integration'),
        }),
        fork_rate_limit: await runCase({
          ...FORK,
          state: 'success',
          error: makeError(403, 'API rate limit exceeded'),
        }),
        fork_rate_limit_failure: await runCase({
          ...FORK,
          state: 'failure',
          error: makeError(403, 'API rate limit exceeded'),
        }),
        fork_rate_limit_error: await runCase({
          ...FORK,
          state: 'error',
          error: makeError(403, 'API rate limit exceeded'),
        }),
        fork_rate_limit_pending: await runCase({
          ...FORK,
          state: 'pending',
          error: makeError(403, 'API rate limit exceeded'),
        }),
        fork_rate_limit_response_message: await runCase({
          ...FORK,
          state: 'success',
          error: makeError(403, 'Forbidden', {}, 'secondary rate limit exceeded'),
        }),
        fork_primary_rate_limit_header: await runCase({
          ...FORK,
          state: 'success',
          error: makeError(403, 'Forbidden', { 'x-ratelimit-remaining': '0' }),
        }),
        fork_secondary_rate_limit_header: await runCase({
          ...FORK,
          state: 'success',
          error: makeError(403, 'Forbidden', { 'retry-after': '60' }),
        }),
        fork_server_error: await runCase({
          ...FORK,
          state: 'success',
          error: makeError(500, 'Internal server error'),
        }),
        fork_permission_404: await runCase({
          ...FORK,
          state: 'success',
          error: makeError(404, 'Resource not accessible by integration'),
        }),
        happy_path: await runCase({ ...FORK, state: 'success', error: null }),
      };
      process.stdout.write(JSON.stringify(outcomes));
    })();
    """).strip()


def _extract_step_script(step_name: str = STEP_NAME) -> str:
    lines = GATE_WORKFLOW.read_text(encoding="utf-8").splitlines()
    step_index = next(
        (index for index, line in enumerate(lines) if line.strip() == f"- name: {step_name}"),
        None,
    )
    if step_index is not None:
        for index in range(step_index + 1, len(lines)):
            line = lines[index]
            if line.strip() == "script: |":
                script_indent = len(line) - len(line.lstrip())
                script_lines: list[str] = []
                for script_line in lines[index + 1 :]:
                    if script_line.strip():
                        indent = len(script_line) - len(script_line.lstrip())
                        if indent <= script_indent:
                            break
                    script_lines.append(script_line[script_indent + 2 :])
                return "\n".join(script_lines)
            if line.strip().startswith("- name:"):
                break
    raise AssertionError(f"{GATE_WORKFLOW} no longer defines {step_name!r}")


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the host
        message = "node is required to execute the Gate github-script step"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)

    workdir = tmp_path_factory.mktemp("gate-status")
    step_path = workdir / "step.js"
    step_path.write_text(_extract_step_script(), encoding="utf-8")
    runner_path = workdir / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")

    completed = subprocess.run(
        [
            node,
            str(runner_path),
            str(step_path),
            str(REPO_ROOT / ".github" / "scripts" / "github-api-with-retry.js"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return dict(json.loads(completed.stdout))


def test_fork_read_only_403_does_not_fail_the_gate(outcomes: dict[str, Any]) -> None:
    assert outcomes["fork_read_only"]["threw"] is None
    assert outcomes["fork_read_only"]["failures"] == []


def test_fork_read_only_403_reports_the_real_verdict(outcomes: dict[str, Any]) -> None:
    case = outcomes["fork_read_only"]
    warning = " ".join(case["warnings"])
    summary = " ".join(case["summaryRaw"])
    assert "read-only" in warning
    assert "'success'" in warning
    assert case["summaryWrites"] == 1
    assert "headsha" in summary
    assert "success" in summary
    assert "all checks passed" in summary


def test_fork_read_only_403_preserves_failure_verdict(
    outcomes: dict[str, Any],
) -> None:
    case = outcomes["fork_read_only_failure"]
    warning = " ".join(case["warnings"])
    summary = " ".join(case["summaryRaw"])
    assert case["threw"] is None
    assert "'failure'" in warning
    assert "failure" in summary
    assert len(case["failures"]) == 1
    assert "'failure'" in case["failures"][0]


@pytest.mark.parametrize("state", ["error", "pending"])
def test_fork_read_only_403_fails_closed_for_other_non_success_verdicts(
    outcomes: dict[str, Any], state: str
) -> None:
    case = outcomes[f"fork_read_only_{state}"]
    assert case["threw"] is None
    assert len(case["failures"]) == 1
    assert f"'{state}'" in case["failures"][0]
    assert f"'{state}'" in " ".join(case["warnings"])
    assert case["summaryWrites"] == 1
    assert f"**{state}**" in " ".join(case["summaryRaw"])


def test_deleted_fork_read_only_403_reports_the_verdict(
    outcomes: dict[str, Any],
) -> None:
    case = outcomes["deleted_fork_read_only"]
    warning = " ".join(case["warnings"])
    assert case["threw"] is None
    assert "deleted source repository" in warning
    assert case["summaryWrites"] == 1


def test_same_repo_403_still_fails_the_gate(outcomes: dict[str, Any]) -> None:
    case = outcomes["same_repo_read_only"]
    assert case["threw"]["status"] == 403
    assert case["summaryWrites"] == 0
    assert case["failures"] == []
    assert not any("read-only" in warning for warning in case["warnings"])


def test_rate_limit_403_keeps_its_own_path(outcomes: dict[str, Any]) -> None:
    for key in (
        "fork_rate_limit",
        "fork_rate_limit_response_message",
        "fork_primary_rate_limit_header",
        "fork_secondary_rate_limit_header",
    ):
        case = outcomes[key]
        assert case["threw"] is None
        assert any("Rate limit" in warning for warning in case["warnings"])
        assert case["summaryWrites"] == 0
        assert case["failures"] == []


@pytest.mark.parametrize("state", ["failure", "error", "pending"])
def test_rate_limit_403_fails_closed_for_non_success_verdicts(
    outcomes: dict[str, Any], state: str
) -> None:
    case = outcomes[f"fork_rate_limit_{state}"]
    assert case["threw"] is None
    assert len(case["failures"]) == 1
    assert f"'{state}'" in case["failures"][0]


def test_non_403_errors_still_fail_the_gate(outcomes: dict[str, Any]) -> None:
    assert outcomes["fork_server_error"]["threw"]["status"] == 500
    assert outcomes["fork_permission_404"]["threw"]["status"] == 404


def test_successful_status_write_is_silent(outcomes: dict[str, Any]) -> None:
    case = outcomes["happy_path"]
    assert case["threw"] is None
    assert case["warnings"] == []
    assert case["failures"] == []
    assert case["summaryWrites"] == 0
    assert case["summaryRaw"] == []
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
    ]


COMMENT_RUNNER_JS = textwrap.dedent("""
    const nodeFs = require('fs');
    const vm = require('vm');
    const src = nodeFs.readFileSync(process.argv[2], 'utf8');

    function makeError(status, message, response = null) {
      const error = new Error(message);
      error.status = status;
      if (response !== null) error.response = response;
      return error;
    }

    async function runCase({ headRepo, baseRepo, error }) {
      const warnings = [];
      const summaryRaw = [];
      const summaryStub = {
        addHeading() { return summaryStub; },
        addRaw(text) { summaryRaw.push(String(text)); return summaryStub; },
        async write() { summaryRaw.push('<written>'); },
      };
      const sandbox = {
        console: { log() {} },
        require: (id) => {
          if (id === 'path') return { resolve: (path) => '/tmp/' + path };
          if (id === 'fs') {
            return {
              existsSync: () => true,
              readFileSync: () => 'GATE SUMMARY BODY',
            };
          }
          return {
            upsertAnchoredComment: async () => { if (error) throw error; },
          };
        },
        core: { warning: (message) => warnings.push(String(message)), summary: summaryStub },
        context: {
          payload: {
            pull_request: {
              number: 1,
              head: { repo: { full_name: headRepo } },
              base: { repo: { full_name: baseRepo } },
            },
          },
        },
        github: {},
      };
      vm.createContext(sandbox);
      let threw = null;
      try {
        await vm.runInContext('(async () => {\\n' + src + '\\n})()', sandbox);
      } catch (error) {
        threw = {
          status: error.status === undefined ? null : error.status,
          message: String(error.message),
        };
      }
      return { warnings, summaryRaw, threw };
    }

    const FORK = {
      headRepo: 'outside-contributor/Orchestrator',
      baseRepo: 'stranske/Orchestrator',
    };
    const SAME = {
      headRepo: 'stranske/Orchestrator',
      baseRepo: 'stranske/Orchestrator',
    };

    (async () => {
      const outcomes = {
        fork_read_only: await runCase({
          ...FORK,
          error: makeError(403, 'Resource not accessible by integration'),
        }),
        same_repo_read_only: await runCase({
          ...SAME,
          error: makeError(403, 'Resource not accessible by integration'),
        }),
        fork_rate_limit: await runCase({
          ...FORK,
          error: makeError(403, 'API rate limit exceeded'),
        }),
        fork_server_error: await runCase({
          ...FORK,
          error: makeError(500, 'Internal server error'),
        }),
        happy_path: await runCase({ ...FORK, error: null }),
      };
      process.stdout.write(JSON.stringify(outcomes));
    })();
    """).strip()


@pytest.fixture(scope="module")
def comment_outcomes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the host
        message = "node is required to execute the Gate github-script step"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)

    workdir = tmp_path_factory.mktemp("gate-comment")
    step_path = workdir / "step.js"
    step_path.write_text(_extract_step_script(COMMENT_STEP_NAME), encoding="utf-8")
    runner_path = workdir / "runner.js"
    runner_path.write_text(COMMENT_RUNNER_JS, encoding="utf-8")

    completed = subprocess.run(
        [node, str(runner_path), str(step_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return dict(json.loads(completed.stdout))


def test_comment_fork_read_only_403_falls_back_to_the_job_summary(
    comment_outcomes: dict[str, Any],
) -> None:
    case = comment_outcomes["fork_read_only"]
    assert case["threw"] is None
    assert any("read-only" in warning for warning in case["warnings"])
    assert "GATE SUMMARY BODY" in " ".join(case["summaryRaw"])
    assert "<written>" in case["summaryRaw"]


def test_comment_same_repo_403_still_fails_the_gate(
    comment_outcomes: dict[str, Any],
) -> None:
    case = comment_outcomes["same_repo_read_only"]
    assert case["threw"]["status"] == 403


def test_comment_rate_limit_403_never_uses_fork_fallback(
    comment_outcomes: dict[str, Any],
) -> None:
    case = comment_outcomes["fork_rate_limit"]
    assert case["threw"]["status"] == 403
    assert case["warnings"] == []
    assert case["summaryRaw"] == []


def test_comment_non_403_still_fails_the_gate(
    comment_outcomes: dict[str, Any],
) -> None:
    case = comment_outcomes["fork_server_error"]
    assert case["threw"]["status"] == 500


def test_comment_happy_path_is_silent(comment_outcomes: dict[str, Any]) -> None:
    case = comment_outcomes["happy_path"]
    assert case["threw"] is None
    assert case["warnings"] == []
    assert case["summaryRaw"] == []
