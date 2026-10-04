"""Prompt authoring separates issue readiness from the worker dispatch contract."""

import json
from pathlib import Path

import pytest

import feedback
import roles

BODY = """## Why
The batch authoring path needs an issue format contract.

## Tasks
- [ ] Add batch authoring in src/roles.py.

## Acceptance Criteria
- python3 -m pytest tests/test_role_prompt_batch.py -q passes; capture stdout in the PR.

## Non-Goals
No surface binding changes.
"""


def proposal(body=BODY):
    return {"summary": "Author one bounded issue.", "issue_body": body, "confidence": "high"}


@pytest.fixture
def private_brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(roles, "_role_capability_event", lambda *a, **kw: None)
    roles.reset_role_invocation_counts()
    return tmp_path


def test_issue_body_mode_validates_format_sections_and_needs_no_task_type(private_brain):
    result = roles.run_prompt_agent(
        target="owner/repo#1",
        goal="Author the issue",
        task_type=None,
        output="issue_body",
        backend="cursor",
        dispatch=True,
        proposal_json=proposal(),
    )
    assert result["errors"] == []
    assert result["issue_body"] == BODY
    assert result["role_run_id"]
    assert result["dispatch_prompt"] is None
    assert "issue authoring" in result["prompt"]
    assert roles._validate_prompt_agent(proposal())  # Dispatch mode retains its stronger contract.


@pytest.mark.parametrize("section", ["Why", "Tasks", "Acceptance Criteria", "Non-Goals"])
def test_issue_body_rejects_missing_format_section(section):
    assert roles._validate_issue_body(proposal(BODY.replace("## " + section, "## Other")))


def test_issue_body_rejects_tasks_without_checkboxes():
    assert roles._validate_issue_body(proposal(BODY.replace("- [ ]", "-")))


def test_batch_routes_once_and_records_one_run_per_item_with_a_shared_batch_id(
    private_brain,
    monkeypatch,
):
    routes = []
    calls = []
    monkeypatch.setattr(
        roles, "route_role", lambda *a, **kw: routes.append(a) or {"agent": "cursor"}
    )

    def offload(backend, prompt, **kwargs):
        calls.append((backend, prompt))
        return {"run_id": f"fake:{len(calls)}", "output": json.dumps(proposal()), "exit": 0}

    monkeypatch.setattr(roles.dispatcher, "offload", offload)
    result = roles.run_prompt_batch(
        [{"target": f"owner/repo#{n}", "goal": f"Issue {n}"} for n in (1, 2)],
        output="issue_body",
        dispatch=True,
    )
    assert len(routes) == 1
    assert len(calls) == 2
    assert all(backend == "cursor" for backend, _ in calls)
    ids = [item["role_run_id"] for item in result["items"]]
    assert len(set(ids)) == 2 and all(ids)
    assert all(not item["errors"] for item in result["items"])
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT run_id,decomposition FROM runs WHERE role_name='prompt'"
        ).fetchall()
    assert {row[0] for row in rows} == set(ids)
    assert {json.loads(row[1])["batch_id"] for row in rows} == {result["batch_id"]}


def test_no_capacity_does_not_route_each_item_or_write_baseline_as_issue(
    private_brain, monkeypatch
):
    routes = []
    monkeypatch.setattr(roles, "route_role", lambda *a, **kw: routes.append(a) and None)
    result = roles.run_prompt_batch(
        [{"target": f"owner/repo#{n}", "goal": "Issue"} for n in (1, 2)],
        output="issue_body",
        dispatch=True,
    )
    assert len(routes) == 1
    manifest = roles.write_prompt_batch(result, private_brain / "bodies")
    assert all(item["errors"] and item["body_file"] is None for item in manifest["items"])


def test_cli_batch_writes_valid_bodies_and_invalid_item_verdicts(
    private_brain, monkeypatch, capsys
):
    items = [
        {"target": "owner/repo#1", "goal": "Good", "proposal_json": proposal()},
        {"target": "owner/repo#2", "goal": "Bad", "proposal_json": proposal("Bad body")},
    ]
    batch = private_brain / "items.json"
    batch.write_text(json.dumps(items))
    directory = private_brain / "bodies"
    assert (
        roles.main(
            [
                "prompt",
                "--batch",
                str(batch),
                "--output",
                "issue_body",
                "--dispatch",
                "--backend",
                "cursor",
                "--output-dir",
                str(directory),
            ]
        )
        == 1
    )
    manifest = json.loads(capsys.readouterr().out)
    assert manifest == json.loads((directory / "manifest.json").read_text())
    assert Path(manifest["items"][0]["body_file"]).read_text() == BODY
    assert manifest["items"][0]["role_run_id"]
    assert manifest["items"][1]["role_run_id"]
    assert manifest["items"][1]["body_file"] is None
    assert not (directory / "002.md").exists()


def test_batch_rejects_invalid_items_before_any_routing(monkeypatch):
    monkeypatch.setattr(roles, "route_role", lambda *a, **kw: pytest.fail("invalid batch routed"))
    with pytest.raises(ValueError):
        roles.run_prompt_batch([{"target": "owner/repo#1"}], output="issue_body")


def test_batch_export_refuses_overwriting_an_existing_directory(private_brain):
    result = roles.run_prompt_batch(
        [{"target": "owner/repo#1", "goal": "Issue", "proposal_json": proposal()}],
        output="issue_body",
        backend="cursor",
        dispatch=True,
    )
    directory = private_brain / "bodies"
    roles.write_prompt_batch(result, directory)
    with pytest.raises(FileExistsError):
        roles.write_prompt_batch(result, directory)
    assert (directory / "001.md").read_text() == BODY
