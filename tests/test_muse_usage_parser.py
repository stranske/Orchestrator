"""Pure parser compatibility controls; no Muse/account/provider process is run."""

import copy
import json
from pathlib import Path

import pytest
from test_muse_backend import _native_muse_events

import adapters


def stream():
    usage = json.loads((Path(__file__).parent / "fixtures/muse-native-run-usage.json").read_text())
    events = _native_muse_events("The original terminal text remains intact.")
    command = usage["payload"]["command_id"]
    session = usage["stream"]["id"]
    for event in events:
        event["stream"]["id"] = session
        event["causation_id"] = command
        event["payload"]["command_id"] = command
        if "run_stream" in event["payload"]:
            event["payload"]["run_stream"]["id"] = command
    return events, usage


def encode(events):
    return "\n".join(json.dumps(event) for event in events)


def test_captured_typed_usage_is_metadata_before_one_original_terminal():
    events, usage = stream()
    before = copy.deepcopy(usage)
    assert (
        adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]]))
        == events[-1]["payload"]["text"]
    )
    assert usage == before
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage])) == ""
    assert adapters.muse_terminal_text(encode(events)) == events[-1]["payload"]["text"]


@pytest.mark.parametrize(
    "path,value",
    [
        (("stream", "id"), "another-session"),
        (("stream", "kind"), "session"),
        (("causation_id",), "another-command"),
        (("record_type",), "event"),
        (("record_type",), "error"),
        (("durability",), "durable"),
        (("payload_type",), "run.usage.unknown"),
        (("schema_version",), True),
        (("payload_schema_version",), "1"),
        (("payload", "kind"), "run_terminal"),
        (("payload", "command_id"), "another-command"),
        (("payload", "run_stream", "id"), "another-run"),
        (("payload", "run_stream", "kind"), "session"),
        (("payload", "children_included"), 0),
        (("payload", "allowance"), {"unreviewed": True}),
        (("payload", "models"), {}),
        (("payload", "calls"), True),
        (("payload", "completed_calls"), 1.0),
        (("payload", "failed_calls"), -1),
        (("payload", "cancelled_calls"), 2**64),
        (("payload", "models", 0, "model"), True),
        (("payload", "models", 0, "input_tokens"), "0"),
        (("payload", "models", 0, "cached_tokens"), -1),
        (("payload", "models", 0, "output_tokens"), False),
        (("payload", "models", 0, "reasoning_tokens"), 2**64),
        (("payload", "models", 0, "calls"), None),
    ],
)
def test_usage_requires_exact_binding_status_and_field_types(path, value):
    events, usage = stream()
    selected = usage
    for key in path[:-1]:
        selected = selected[key]
    selected[path[-1]] = value
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""


@pytest.mark.parametrize(
    "field",
    [
        "kind",
        "command_id",
        "run_stream",
        "models",
        "calls",
        "completed_calls",
        "failed_calls",
        "cancelled_calls",
        "children_included",
        "allowance",
    ],
)
def test_usage_missing_required_payload_field_is_not_ignored(field):
    events, usage = stream()
    del usage["payload"][field]
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""


@pytest.mark.parametrize("where", ["payload", "model"])
def test_unknown_usage_body_fields_fail_closed(where):
    events, usage = stream()
    selected = usage["payload"] if where == "payload" else usage["payload"]["models"][0]
    selected["invented"] = True
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""


@pytest.mark.parametrize("position", [0, 1, 2, 4])
def test_usage_requires_already_started_run_and_cannot_follow_terminal(position):
    events, usage = stream()
    events.insert(position, usage)
    assert adapters.muse_terminal_text(encode(events)) == ""


def test_usage_never_repairs_conflicting_or_duplicate_terminal():
    events, usage = stream()
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1], events[-1]])) == ""
    events[-1]["payload"]["terminal"] = "failed"
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""


def test_usage_models_have_the_reviewed_finite_projection_bound():
    events, usage = stream()
    usage["payload"]["models"] *= 129
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""
