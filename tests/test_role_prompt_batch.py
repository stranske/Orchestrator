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


def test_issue_body_mode_validates_format_sections_and_needs_no_task_type(
    private_brain, monkeypatch
):
    routes = []
    calls = []
    backend_body = BODY
    monkeypatch.setattr(
        roles, "route_role", lambda *a, **kw: routes.append(a) or {"agent": "cursor"}
    )

    def offload(backend, prompt, **kwargs):
        calls.append((backend, prompt))
        return {
            "run_id": f"fake:{len(calls)}",
            "output": json.dumps(proposal(backend_body)),
            "exit": 0,
        }

    monkeypatch.setattr(roles.dispatcher, "offload", offload)
    result = roles.run_prompt_agent(
        target="owner/repo#1",
        goal="Author the issue",
        output="issue_body",
        dispatch=True,
    )
    assert result["errors"] == []
    assert result["issue_body"] == BODY
    assert result["role_run_id"]
    assert result["dispatch_prompt"] is None
    assert result["baseline_prompt"] is None
    assert "issue authoring" in result["prompt"]
    assert result["backend_run_id"] == "fake:1"
    assert result["decision_source"] == "prompt_agent"
    assert result["role_record_error"] is None
    assert routes == [("prompt",)]
    assert calls == [("cursor", result["prompt"])]
    # Both omitting task_type and explicitly supplying None must accept the backend output.
    explicit_none = roles.run_prompt_agent(
        target="owner/repo#2",
        goal="Author another issue",
        task_type=None,
        output="issue_body",
        dispatch=True,
    )
    assert explicit_none["errors"] == []
    assert explicit_none["issue_body"] == BODY
    assert explicit_none["backend_run_id"] == "fake:2"
    assert explicit_none["role_run_id"] != result["role_run_id"]
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT run_id,decomposition FROM runs WHERE role_name='prompt'"
        ).fetchall()
    recorded = {run_id: json.loads(metadata) for run_id, metadata in rows}
    assert set(recorded) == {result["role_run_id"], explicit_none["role_run_id"]}
    for item in (result, explicit_none):
        assert recorded[item["role_run_id"]]["backend_run_id"] == item["backend_run_id"]
        assert recorded[item["role_run_id"]]["proposal"] == proposal()
    # Check the format contract through the backend path, not just the validator helper.
    for section in ("Why", "Tasks", "Acceptance Criteria", "Non-Goals"):
        backend_body = BODY.replace("## " + section, "## Other")
        rejected = roles.run_prompt_agent(
            target="owner/repo#3",
            goal="Reject an incomplete issue",
            output="issue_body",
            dispatch=True,
        )
        assert f"issue_body requires a non-empty ## {section} section" in rejected["errors"]
        assert rejected["proposal"] is None
        assert rejected["issue_body"] is None
        assert rejected["dispatch_prompt"] is None
        assert rejected["role_record_error"] is None
        assert rejected["role_run_id"]
        with feedback._conn() as conn:
            metadata = json.loads(
                conn.execute(
                    "SELECT decomposition FROM runs WHERE run_id=?",
                    (rejected["role_run_id"],),
                ).fetchone()[0]
            )
        assert metadata["backend_run_id"] == rejected["backend_run_id"]
        assert "proposal" not in metadata
    assert roles._validate_prompt_agent(proposal())  # Dispatch mode retains its stronger contract.


@pytest.mark.parametrize("section", ["Why", "Tasks", "Acceptance Criteria", "Non-Goals"])
def test_issue_body_rejects_missing_format_section(section):
    errors = roles._validate_issue_body(proposal(BODY.replace("## " + section, "## Other")))
    assert f"issue_body requires a non-empty ## {section} section" in errors
    # A present but empty section is also invalid, even when the next section has content.
    start = BODY.index("## " + section)
    end = BODY.find("\n## ", start + 1)
    empty_body = BODY[:start] + "## " + section + "\n" + (BODY[end:] if end != -1 else "")
    assert f"issue_body requires a non-empty ## {section} section" in roles._validate_issue_body(
        proposal(empty_body)
    )


def test_issue_body_rejects_tasks_without_checkboxes():
    for marker in ("-", "- [x]"):
        assert roles._validate_issue_body(proposal(BODY.replace("- [ ]", marker))) == [
            "issue_body Tasks requires unchecked task checkboxes"
        ]


def test_batch_routes_once_and_records_one_run_per_item_with_a_shared_batch_id(
    private_brain,
    monkeypatch,
):
    routes = []
    calls = []
    bodies = [BODY.replace("Add batch authoring", f"Author issue {n}") for n in (1, 2)]
    monkeypatch.setattr(
        roles, "route_role", lambda *a, **kw: routes.append(a) or {"agent": "cursor"}
    )

    def offload(backend, prompt, **kwargs):
        calls.append((backend, prompt))
        return {
            "run_id": f"fake:{len(calls)}",
            "output": json.dumps(proposal(bodies[len(calls) - 1])),
            "exit": 0,
        }

    monkeypatch.setattr(roles.dispatcher, "offload", offload)
    result = roles.run_prompt_batch(
        [{"target": f"owner/repo#{n}", "goal": f"Issue {n}"} for n in (1, 2)],
        output="issue_body",
        dispatch=True,
    )
    assert routes == [("prompt",)]
    assert len(calls) == 2
    assert all(backend == "cursor" for backend, _ in calls)
    for index, (_, prompt) in enumerate(calls, 1):
        context = json.loads(prompt.splitlines()[-1])
        assert context["target"] == f"owner/repo#{index}"
        assert context["goal"] == f"Issue {index}"
    ids = [item["role_run_id"] for item in result["items"]]
    assert len(set(ids)) == 2 and all(ids)
    assert all(not item["errors"] for item in result["items"])
    assert [item["issue_body"] for item in result["items"]] == bodies
    assert [item["backend_run_id"] for item in result["items"]] == ["fake:1", "fake:2"]
    assert all(item["role_record_error"] is None for item in result["items"])
    assert all(item["batch_id"] == result["batch_id"] for item in result["items"])
    assert all(item["routing"] == result["routing"] for item in result["items"])
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT run_id,target,agent,decomposition FROM runs WHERE role_name='prompt'"
        ).fetchall()
    assert {row[0] for row in rows} == set(ids)
    assert len(rows) == len(result["items"])
    recorded = {
        run_id: (target, agent, json.loads(metadata)) for run_id, target, agent, metadata in rows
    }
    for item in result["items"]:
        target, agent, metadata = recorded[item["role_run_id"]]
        assert target == item["target"]
        assert agent == "cursor"
        assert metadata["batch_id"] == result["batch_id"]
        assert metadata["backend_run_id"] == item["backend_run_id"]
        assert metadata["proposal"] == proposal(item["issue_body"])
        assert metadata["decision_source"] == "prompt_agent"


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
    assert manifest["output"] == "issue_body" and manifest["batch_id"]
    assert Path(manifest["items"][0]["body_file"]).read_text() == BODY
    assert manifest["items"][0]["valid"] is True
    assert manifest["items"][0]["errors"] == []
    assert manifest["items"][0]["role_run_id"]
    assert manifest["items"][1]["role_run_id"]
    assert manifest["items"][1]["body_file"] is None
    assert manifest["items"][1]["valid"] is False
    assert manifest["items"][1]["errors"]
    assert not (directory / "002.md").exists()


def test_batch_rejects_invalid_items_before_any_routing(monkeypatch):
    monkeypatch.setattr(roles, "route_role", lambda *a, **kw: pytest.fail("invalid batch routed"))
    with pytest.raises(ValueError):
        roles.run_prompt_batch([{"target": "owner/repo#1"}], output="issue_body")


def test_batch_export_refuses_overwriting_an_existing_directory(private_brain, monkeypatch):
    result = roles.run_prompt_batch(
        [{"target": "owner/repo#1", "goal": "Issue", "proposal_json": proposal()}],
        output="issue_body",
        backend="cursor",
        dispatch=True,
    )
    directory = private_brain / "bodies"
    roles.write_prompt_batch(result, directory)
    original_manifest = (directory / "manifest.json").read_text()
    with pytest.raises(FileExistsError):
        roles.write_prompt_batch(result, directory)
    assert (directory / "001.md").read_text() == BODY
    # The CLI must refuse before routing or calling a backend, not just at export time.
    monkeypatch.setattr(
        roles, "run_prompt_batch", lambda *a, **kw: pytest.fail("existing output routed")
    )
    with pytest.raises(SystemExit) as exc:
        roles.main(["prompt", "--batch", "missing.json", "--output-dir", str(directory)])
    assert exc.value.code == 2
    assert (directory / "manifest.json").read_text() == original_manifest
