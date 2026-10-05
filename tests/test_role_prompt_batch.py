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
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **kw: None)
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
        roles.dispatcher,
        "build_prompt",
        lambda *a, **kw: pytest.fail("issue authoring constructed a worker dispatch prompt"),
    )
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
    # Exercise every format rejection through the backend path and role recording.
    invalid_bodies = []
    for section in ("Why", "Tasks", "Acceptance Criteria", "Non-Goals"):
        error = f"issue_body requires a non-empty ## {section} section"
        invalid_bodies.append((BODY.replace("## " + section, "## Other"), error))
        start = BODY.index("## " + section)
        end = BODY.find("\n## ", start + 1)
        empty_body = BODY[:start] + "## " + section + "\n" + (BODY[end:] if end != -1 else "")
        invalid_bodies.append((empty_body, error))
    for marker in ("-", "- [x]"):
        invalid_bodies.append(
            (BODY.replace("- [ ]", marker), "issue_body Tasks requires unchecked task checkboxes")
        )
    for backend_body, error in invalid_bodies:
        rejected = roles.run_prompt_agent(
            target="owner/repo#3",
            goal="Reject an incomplete issue",
            output="issue_body",
            dispatch=True,
        )
        assert error in rejected["errors"]
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

    # Sharing a routing decision must still allow each authored issue to earn its own outcome.
    expected_outcomes = {}
    for index, (item, verdict) in enumerate(zip(result["items"], ("PASS", "FAIL")), 1):
        downstream = f"work:batch-item:{index}"
        feedback.record_run(
            downstream,
            item["target"],
            "implement",
            "cursor",
            influenced_by_role_run_ids=[item["role_run_id"]],
        )
        feedback.record_outcome(
            downstream,
            adjudicated_verdict=verdict,
            merged=verdict == "PASS",
            ci_status="success" if verdict == "PASS" else "failure",
            durability="durable" if verdict == "PASS" else "reverted",
        )
        expected_outcomes[item["role_run_id"]] = (
            "role:prompt",
            verdict,
            int(verdict == "PASS"),
            "success" if verdict == "PASS" else "failure",
            "durable" if verdict == "PASS" else "reverted",
        )
        with feedback._conn() as conn:
            outcomes = conn.execute(
                "SELECT r.run_id,r.task_type,o.adjudicated_verdict,o.merged,"
                "o.ci_status,o.durability FROM runs r JOIN outcomes o USING(run_id) "
                "WHERE r.role_name='prompt'"
            ).fetchall()
        assert {run_id: tuple(values) for run_id, *values in outcomes} == expected_outcomes


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


def test_issue_body_confidence_errors_do_not_abort_later_batch_items(private_brain):
    for confidence in ([], {}, None, 1):
        # These are independent authoring cycles, not four batches in one cycle.
        roles.reset_role_invocation_counts()
        result = roles.run_prompt_batch(
            [
                {
                    "target": "owner/repo#1",
                    "goal": "Bad",
                    "proposal_json": {
                        **proposal(),
                        "confidence": confidence,
                    },
                },
                {"target": "owner/repo#2", "goal": "Good", "proposal_json": proposal()},
            ],
            output="issue_body",
            backend="cursor",
            dispatch=True,
        )
        assert result["items"][0]["errors"] == ["confidence must be low, medium or high"]
        assert result["items"][0]["issue_body"] is None
        assert result["items"][1]["issue_body"] == BODY
        assert all(item["role_run_id"] for item in result["items"])


def test_issue_body_accepts_crlf_sections():
    assert roles._validate_issue_body(proposal(BODY.replace("\n", "\r\n"))) == []


def test_batch_optional_fields_are_validated_before_any_item_runs(monkeypatch):
    monkeypatch.setattr(roles, "route_role", lambda *a, **kw: pytest.fail("invalid batch routed"))
    monkeypatch.setattr(roles, "run_prompt_agent", lambda *a, **kw: pytest.fail("item ran"))
    invalid = {
        "task_type": [],
        "target_detail": [],
        "context": [],
        "repo": [],
        "lane": [],
        "acceptance_criteria": "text",
        "constraints": {},
        "expected_paths": 1,
        "proposal_json": [],
    }
    for output in ("dispatch_prompt", "issue_body"):
        for key, value in invalid.items():
            with pytest.raises(ValueError, match=f"batch item {key} must be"):
                roles.run_prompt_batch(
                    [
                        {"target": "owner/repo#1", "goal": "Good"},
                        {"target": "owner/repo#2", "goal": "Bad", key: value},
                    ],
                    output=output,
                    dispatch=True,
                )


def test_replay_requires_backend_only_when_dispatched(private_brain, monkeypatch):
    monkeypatch.setattr(roles, "route_role", lambda *a, **kw: None)
    monkeypatch.setattr(
        roles.dispatcher, "offload", lambda *a, **kw: pytest.fail("replay offloaded")
    )
    items = [{"target": "owner/repo#1", "goal": "Replay", "proposal_json": proposal()}]
    rejected = roles.run_prompt_batch(items, output="issue_body", dispatch=True)
    assert rejected["items"][0]["errors"] == [
        "no eligible backend has capacity for the prompt role"
    ]
    manifest = roles.write_prompt_batch(rejected, private_brain / "rejected")
    assert manifest["items"][0]["valid"] is False
    assert manifest["items"][0]["body_file"] is None
    assert manifest["items"][0]["role_run_id"] is None
    preview = roles.run_prompt_batch(items, output="issue_body")
    assert preview["items"][0]["errors"] == []
    assert preview["items"][0]["issue_body"] == BODY
    replay = roles.run_prompt_batch(items, output="issue_body", dispatch=True, backend="cursor")
    assert replay["items"][0]["errors"] == []
    assert replay["items"][0]["role_run_id"]


def test_batch_cli_uses_utf8_for_input_bodies_and_manifest(private_brain, monkeypatch, capsys):
    body = BODY.replace("batch authoring", "café authoring — 日本語")
    batch = private_brain / "unicode.json"
    directory = private_brain / "unicode-bodies"
    batch.write_text(
        json.dumps(
            [{"target": "owner/repo#1", "goal": "日本語", "proposal_json": proposal(body)}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    read_text, write_text = Path.read_text, Path.write_text
    calls = []

    def read(path, *args, **kwargs):
        if path == batch:
            assert kwargs.get("encoding") == "utf-8"
            calls.append("input")
        return read_text(path, *args, **kwargs)

    def write(path, *args, **kwargs):
        if path.parent == directory:
            assert kwargs.get("encoding") == "utf-8"
            calls.append(path.name)
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", write)
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
        == 0
    )
    assert calls == ["input", "001.md", "manifest.json"]
    assert (directory / "001.md").read_bytes().decode("utf-8") == body
    assert json.loads(capsys.readouterr().out)["items"][0]["valid"] is True


@pytest.mark.parametrize("failure", ["backend", "validation", "recording"])
def test_single_issue_cli_failure_withholds_body(private_brain, monkeypatch, capsys, failure):
    monkeypatch.setattr(
        roles.dispatcher,
        "offload",
        lambda *a, **kw: {
            "run_id": "fake:cli",
            "output": json.dumps(proposal("Bad" if failure == "validation" else BODY)),
            "exit": 1 if failure == "backend" else 0,
        },
    )
    if failure == "recording":

        def fail_record(*args, **kwargs):
            raise RuntimeError("role recording failed")

        monkeypatch.setattr(feedback, "record_role_run", fail_record)
    args = [
        "prompt",
        "--target",
        "owner/repo#1",
        "--goal",
        "Issue",
        "--output",
        "issue_body",
        "--dispatch",
        "--backend",
        "cursor",
    ]
    assert roles.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
    assert roles.main(args + ["--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["errors"] or json.loads(captured.out)["role_record_error"]
    assert captured.err.strip()


def test_batch_recording_failure_withholds_body(private_brain, monkeypatch):
    def fail_record(*args, **kwargs):
        raise RuntimeError("role recording failed")

    monkeypatch.setattr(feedback, "record_role_run", fail_record)
    result = roles.run_prompt_batch(
        [{"target": "owner/repo#1", "goal": "Issue", "proposal_json": proposal()}],
        output="issue_body",
        dispatch=True,
        backend="cursor",
    )
    manifest = roles.write_prompt_batch(result, private_brain / "recording-failed")
    assert manifest["items"][0]["valid"] is False
    assert manifest["items"][0]["body_file"] is None
    assert manifest["items"][0]["role_record_error"] == "role recording failed"


def test_batch_cli_without_dispatch_emits_manifest_only(private_brain, monkeypatch, capsys):
    monkeypatch.setattr(
        roles.dispatcher, "offload", lambda *a, **kw: pytest.fail("preview offloaded")
    )
    batch = private_brain / "preview.json"
    directory = private_brain / "preview"
    batch.write_text(json.dumps([{"target": "owner/repo#1", "goal": "Issue"}]), encoding="utf-8")
    assert (
        roles.main(
            [
                "prompt",
                "--batch",
                str(batch),
                "--output",
                "issue_body",
                "--backend",
                "cursor",
                "--output-dir",
                str(directory),
            ]
        )
        == 0
    )
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["items"][0]["body_file"] is None
    assert manifest["items"][0]["role_run_id"] is None
    assert "prompt" not in manifest["items"][0]
    assert list(directory.iterdir()) == [directory / "manifest.json"]
