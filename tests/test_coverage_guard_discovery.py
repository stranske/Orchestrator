"""Executable contract for Maint Coverage Guard's workflow-run discovery."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import paths

GUARD = paths.REPO_ROOT / ".github/workflows/maint-coverage-guard.yml"
DEFAULT_WORKFLOW = ".github/workflows/pr-00-gate.yml"


def _discover_script() -> str:
    workflow = GUARD.read_text(encoding="utf-8")
    step = workflow.index("      - name: Locate latest Gate workflow run\n")
    marker = "          script: |\n"
    start = workflow.index(marker, step) + len(marker)
    end = workflow.index("\n      - name:", start)
    return textwrap.dedent(workflow[start:end])


def _run_discovery(tmp_path: Path, config: str | None) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    if config is not None:
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "coverage-baseline.json").write_text(config, encoding="utf-8")

    node = shutil.which("node")
    assert node, "Node.js is required to execute the actions/github-script discovery block"
    script = _discover_script()
    harness = f"""
const context = {{ repo: {{ owner: 'stranske', repo: 'Orchestrator' }} }};
const calls = [];
const messages = [];
const outputs = {{}};
const actions = {{
  listWorkflowRuns: Symbol('listWorkflowRuns'),
  listWorkflowRunArtifacts: Symbol('listWorkflowRunArtifacts'),
}};
const github = {{
  rest: {{ actions }},
  paginate: async (method, params) => {{
    if (method === actions.listWorkflowRuns) {{
      calls.push(params.workflow_id);
      return [{{
        id: calls.length,
        run_number: calls.length,
        conclusion: 'success',
        created_at: `2026-09-0${{calls.length}}T00:00:00Z`,
        html_url: `https://example.invalid/runs/${{calls.length}}`,
      }}];
    }}
    if (method === actions.listWorkflowRunArtifacts) {{
      return [
        {{ name: 'gate-coverage', expired: false }},
        {{ name: 'gate-coverage-trend', expired: false }},
      ];
    }}
    throw new Error('unexpected GitHub API method');
  }},
}};
const core = {{
  info: (message) => messages.push(['info', String(message)]),
  warning: (message) => messages.push(['warning', String(message)]),
  notice: (message) => messages.push(['notice', String(message)]),
  setOutput: (name, value) => {{ outputs[name] = value; }},
  setFailed: (message) => {{ throw new Error(`setFailed: ${{message}}`); }},
}};

(async () => {{
{textwrap.indent(script, '  ')}
  process.stdout.write(JSON.stringify({{ calls, messages, outputs }}));
}})().catch((error) => {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_guard_discovers_runs_from_configured_source_workflows(tmp_path):
    """Config wins, every configured workflow is pooled, and bad config falls back."""
    configured = _run_discovery(
        tmp_path / "configured",
        json.dumps({"source_workflows": [".github/workflows/ci.yml"]}),
    )
    assert configured["calls"] == [".github/workflows/ci.yml"]
    assert DEFAULT_WORKFLOW not in configured["calls"]

    pooled = _run_discovery(
        tmp_path / "pooled",
        json.dumps(
            {
                "source_workflows": [
                    ".github/workflows/ci.yml",
                    ".github/workflows/nightly.yml",
                ]
            }
        ),
    )
    assert pooled["calls"] == [
        ".github/workflows/ci.yml",
        ".github/workflows/nightly.yml",
    ]

    missing = _run_discovery(tmp_path / "missing", None)
    malformed = _run_discovery(tmp_path / "malformed", "{not-json")
    assert missing["calls"] == [DEFAULT_WORKFLOW]
    assert malformed["calls"] == [DEFAULT_WORKFLOW]
