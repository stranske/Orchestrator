"""Offline strict-response contracts; these tests never launch a provider."""

import json

import pytest

import closer_dispatcher as closer


@pytest.mark.parametrize(
    "document",
    [
        '{"classification":"uncertain","classification":"relevant"}',
        '{"decision":{"criterion":"disputed","criterion":"satisfied"}}',
        '{"score":NaN}',
        '{"score":Infinity}',
        '{"score":-Infinity}',
        '{"score":1e999}',
    ],
    ids=["duplicate-root", "duplicate-nested", "nan", "infinity", "negative-infinity", "overflow"],
)
def test_response_parser_rejects_noncanonical_json(document):
    assert closer._parse_low_proposal(document) == {}


@pytest.mark.parametrize(
    "document",
    [
        '{"classification":"relevant"} trailing',
        '{"classification":"relevant"}{"classification":"uncertain"}',
        'prefix {"classification":"relevant"}',
        '{"classification":',
        '[{"classification":"relevant"}]',
        '```json\n{"classification":"relevant"}\n```\nexplanation',
    ],
    ids=["trailing", "two-documents", "prefix", "partial", "array", "fence-trailing"],
)
def test_response_parser_rejects_ambiguous_or_partial_output(document):
    assert closer._parse_low_proposal(document) == {}


@pytest.mark.parametrize("fenced", [False, True])
def test_response_parser_preserves_complete_document(fenced):
    expected = {"classification": "uncertain", "findings": [{"finding_id": "offline:1"}]}
    document = json.dumps(expected)
    if fenced:
        document = "```json\n" + document + "\n```"
    assert closer._parse_low_proposal(document) == expected


@pytest.mark.parametrize(
    "ending", ["cancelled", "failed", "missing"], ids=["cancelled", "failed", "incomplete"]
)
def test_codex_stream_without_completed_turn_is_not_a_deliverable(ending):
    events = [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"classification":"relevant"}'},
        }
    ]
    if ending != "missing":
        events.append({"type": "turn." + ending})
    result = {"output": "\n".join(json.dumps(event) for event in events)}
    assert closer._worker_proposal(result, agent="codex") == {}


def test_codex_tool_output_never_becomes_deliverable():
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "aggregated_output": '{"classification":"relevant"}',
            },
        },
        {"type": "turn.completed"},
    ]
    result = {"output": "\n".join(json.dumps(event) for event in events)}
    assert closer._worker_proposal(result, agent="codex") == {}


@pytest.mark.parametrize(
    "envelope",
    [
        '{"type":"turn.failed","type":"turn.completed"}',
        '{"type":"turn.completed","usage":{"tokens":NaN}}',
        '{"type":"turn.completed","usage":{"tokens":1e999}}',
    ],
    ids=["duplicate-event-key", "nonfinite-event", "overflow-event"],
)
def test_codex_event_envelope_uses_strict_parser(envelope):
    message = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok":true}'}}
    )
    assert closer._worker_proposal({"output": message + "\n" + envelope}, agent="codex") == {}


@pytest.mark.parametrize(
    "before,after",
    [
        ([{"type": "turn.cancelled"}], []),
        ([{"type": "turn.failed"}], []),
        ([{"type": "turn.completed"}], []),
        ([], [{"type": "turn.started"}]),
        ([], [{"type": "turn.completed"}]),
    ],
    ids=["prior-cancel", "prior-failure", "two-turns", "trailing-event", "two-completions"],
)
def test_codex_mixed_or_trailing_turns_are_rejected(before, after):
    message = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": '{"classification":"uncertain"}'},
    }
    events = before + [message, {"type": "turn.completed"}] + after
    output = "\n".join(json.dumps(event) for event in events)
    assert closer._worker_proposal({"output": output}, agent="codex") == {}


def test_codex_completed_single_turn_preserves_final_deliverable():
    expected = {"classification": "uncertain", "findings": [{"finding_id": "offline:1"}]}
    events = [
        {"type": "thread.started", "thread_id": "offline-thread"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(expected)}},
        {"type": "turn.completed"},
    ]
    output = "\n".join(json.dumps(event) for event in events)
    assert closer._worker_proposal({"output": output}, agent="codex") == expected
