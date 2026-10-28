"""Fixed diagnostic DATA and actual queue/API errors; no provider or native effects."""

import json
import time

import pytest
from test_closer_queue import _run, _snapshot

import closer_coding_worker as worker
import closer_dispatcher as closer
import closer_queue as queue

SECRET = "SYNTHETIC_NEVER_EXPORT_credential_query_body"


@pytest.mark.parametrize(
    "method,endpoint,kind,path",
    [
        (
            "GET",
            "repos/secret-owner/private-repo/issues/3?token=" + SECRET,
            "issues",
            "/repos/{owner}/{repo}/issues/3",
        ),
        (
            "GET",
            "/repos/acme/repo/issues/9/timeline?per_page=100&page=2",
            "issues_timeline",
            "/repos/{owner}/{repo}/issues/9/timeline",
        ),
        (
            "GET",
            "repos/acme/repo/issues/comments/88#" + SECRET,
            "issues_comments",
            "/repos/{owner}/{repo}/issues/comments/88",
        ),
        (
            "GET",
            "repos/acme/repo/contents/" + SECRET,
            "contents",
            "/repos/{owner}/{repo}/contents/{path}",
        ),
        (
            "GET",
            "repos/acme/repo/git/ref/heads/" + SECRET,
            "git_ref",
            "/repos/{owner}/{repo}/git/ref/{ref}",
        ),
        (
            "GET",
            "repos/acme/repo/collaborators/" + SECRET + "/permission",
            "collaborator_permission",
            "/repos/{owner}/{repo}/collaborators/{login}/permission",
        ),
        ("POST", "graphql", "graphql", "/graphql"),
        (
            SECRET,
            "https://user:" + SECRET + "@api.github.com/repos/acme/repo/issues/3",
            "unclassified",
            "/{unclassified}",
        ),
        ("GET", "repos/acme/repo/issues/3/" + SECRET, "unclassified", "/{unclassified}"),
        ("GET", "repos/acme/repo/issues/12345678901234567890", "unclassified", "/{unclassified}"),
        ("GET", SECRET * 1000, "unclassified", "/{unclassified}"),
        ("GET", {"secret": SECRET}, "unclassified", "/{unclassified}"),
    ],
)
def test_closed_route_templates_never_export_dynamic_text(method, endpoint, kind, path):
    row = queue._safe_read_endpoint(method, endpoint)
    assert row[1:] == (kind, path)
    assert SECRET not in json.dumps(row)
    assert "secret-owner" not in json.dumps(row) and "private-repo" not in json.dumps(row)
    assert "?" not in row[2] and "#" not in row[2]


def failing_round(
    tmp_path,
    monkeypatch,
    *,
    phase="before_throttle",
    wrapped=True,
    action="correction_coding",
    corrupt=None
):
    state = tmp_path / "queue.json"
    calls = []

    def execute(target, action, snapshot, operation):
        client = closer.GitHub(
            deadline=time.time() + 1, api_fn=lambda *a, **kw: calls.append((a, kw))
        )
        if phase == "after_throttle":
            client.deadline = time.time() + 3

            def throttle(*a, **kw):
                client.deadline = time.time() + 1
                return {"action": "allow"}

            monkeypatch.setattr(closer.gh_capacity, "throttle", throttle)
        try:
            client.api(
                "repos/secret-owner/private-repo/issues/3?token=" + SECRET,
                fields={"Authorization": SECRET, "body": SECRET},
            )
        except closer.RoundDeadlineExhausted:
            if corrupt:
                value = queue._read_timing()
                assert value is not None and value.failure is not None
                corrupt(value.failure)
            if wrapped:
                raise ValueError("different wrapper text " + SECRET) from None
            raise

    receipt = _run(
        state,
        ["acme/repo#3"],
        lambda target: _snapshot("r1", action),
        execute,
        max_passes=1,
        max_workers=1,
    )
    events = [e for e in receipt["events"] if e["kind"] == "execute_uncertain"]
    assert len(events) == 1
    raw = json.loads(state.read_bytes())
    operation = raw["items"]["acme/repo#3"]["operation"]
    assert operation["status"] == "uncertain" and operation["attempts"] == 1
    assert operation["next_trigger"] == "reconcile_operation_before_retry"
    assert calls == []
    assert SECRET not in json.dumps(events)
    return events[0], operation


@pytest.mark.parametrize("phase", ["before_throttle", "after_throttle"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_actual_api_to_queue_preserves_safe_route_through_original_or_wrapped_error(
    tmp_path, monkeypatch, phase, wrapped
):
    event, operation = failing_round(tmp_path, monkeypatch, phase=phase, wrapped=wrapped)
    diag = event["read_only_deadline_diagnostic"]
    assert diag["phase"] == phase and diag["method"] == "GET"
    assert (
        diag["endpoint_kind"] == "issues"
        and diag["sanitized_path"] == "/repos/{owner}/{repo}/issues/3"
    )
    assert diag["same_exception_as_callback"] is (not wrapped)
    assert diag["authority"] is False and diag["retry_authorized"] is False
    assert diag["api_calls"] == 1 and diag["remaining_seconds"] < 2
    assert diag["coding_waypoints"] == []


@pytest.mark.parametrize("action", ["collect_evidence", "repair"])
def test_no_extra_diagnostic_outside_coding_callback(tmp_path, monkeypatch, action):
    event, _ = failing_round(tmp_path, monkeypatch, action=action)
    assert "read_only_deadline_diagnostic" not in event


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_secret",
        "path_secret",
        "nan",
        "bool_counter",
        "unknown_method",
        "unknown_kind",
        "point_secret",
    ],
)
def test_malformed_private_metadata_is_omitted_without_replacing_unknown(
    tmp_path, monkeypatch, mutation
):
    def corrupt(d):
        if mutation == "extra_secret":
            d["Authorization"] = SECRET
        elif mutation == "path_secret":
            d["sanitized_path"] = "/repos/{owner}/{repo}/issues/" + SECRET
        elif mutation == "nan":
            d["remaining_seconds"] = float("nan")
        elif mutation == "bool_counter":
            d["api_calls"] = True
        elif mutation == "unknown_method":
            d["method"] = SECRET
        elif mutation == "unknown_kind":
            d["endpoint_kind"] = SECRET
        else:
            d["coding_waypoints"] = [{"stage": SECRET}]

    event, _ = failing_round(tmp_path, monkeypatch, corrupt=corrupt)
    assert "read_only_deadline_diagnostic" not in event


def test_helpers_cannot_emit_from_foreign_source_or_ended_lifetime():
    operation = {
        "action": "correction_coding",
        "request_started_at": time.time(),
        "deadline": time.time() + 20,
        "id": "synthetic",
    }
    with queue._callback_invocation("acme/repo#3", operation, time.time):
        assert queue._read_timing_start("GET", "repos/acme/repo/issues/3") is None
        queue._coding_timing_point("coding_entry")
        assert queue._read_timing().points == []
        client = closer.GitHub(
            deadline=time.time() + 1, api_fn=lambda *a, **kw: pytest.fail("no call")
        )
        with pytest.raises(closer.RoundDeadlineExhausted) as caught:
            client.api("repos/acme/repo/issues/3")
        assert queue._native_read_failure(caught.value) is None
        queue._read_timing().thread = -1
        assert queue._read_timing() is None
    assert queue._read_timing() is None


def test_actual_coding_entry_emits_only_fixed_waypoint_before_root_work():
    class Stop(Exception):
        pass

    class Root:
        def check(self):
            raise Stop

    operation = {
        "action": "correction_coding",
        "request_started_at": time.time(),
        "deadline": time.time() + 20,
        "id": "synthetic",
    }
    with queue._callback_invocation("acme/repo#3", operation, time.time):
        with pytest.raises(Stop):
            worker._execute(Root(), None, operation)
        points = queue._read_timing().points
        assert len(points) == 1 and points[0]["stage"] == "coding_entry"
        assert points[0]["api_calls"] == 0 and points[0]["api_seconds"] == 0


def test_diagnostic_initialization_failure_does_not_replace_original_refusal(tmp_path, monkeypatch):
    def broken(self, current):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(queue._ReadTiming, "__init__", broken)
    event, _ = failing_round(tmp_path, monkeypatch)
    assert "read_only_deadline_diagnostic" not in event


def test_successful_api_totals_precede_exact_failure_without_retaining_payload(monkeypatch):
    operation = {
        "action": "correction_coding",
        "request_started_at": time.time(),
        "deadline": time.time() + 20,
        "id": "synthetic",
    }
    monkeypatch.setattr(closer.gh_capacity, "throttle", lambda *a, **kw: {"action": "allow"})
    with queue._callback_invocation("acme/repo#3", operation, time.time):
        client = closer.GitHub(
            deadline=time.time() + 20, api_fn=lambda *a, **kw: (0, {"private": SECRET})
        )
        assert client.api("repos/acme/repo/issues/4") == {"private": SECRET}
        assert client.api("repos/acme/repo/issues/5") == {"private": SECRET}
        client.deadline = time.time() + 1
        with pytest.raises(closer.RoundDeadlineExhausted):
            client.api("repos/acme/repo/issues/3?password=" + SECRET)
        diag = queue._validated_read_payload(queue._read_timing().failure)
        assert diag["api_calls"] == 3 and diag["completed_api_seconds"] >= 0
        assert SECRET not in json.dumps(diag)


@pytest.mark.parametrize("action", ["correction_coding", "collect_evidence"])
def test_capture_failure_retains_only_type_and_does_not_replace_unknown(
    tmp_path, monkeypatch, action
):
    def broken_capture(_frame):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(queue, "_read_api_caller", broken_capture)
    event, operation = failing_round(tmp_path, monkeypatch, action=action)
    assert operation["status"] == "uncertain"
    assert "read_only_deadline_diagnostic" not in event
    if action == "correction_coding":
        assert event["read_timing_capture_error"] == {
            "error_type": "RuntimeError",
            "authority": False,
            "retry_authorized": False,
        }
    else:
        assert "read_timing_capture_error" not in event
    assert SECRET not in json.dumps(event)
