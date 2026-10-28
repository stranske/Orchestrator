"""Pure native allowance DATA compatibility; no provider or account authority."""

import copy
import json

import pytest
from test_muse_usage_parser import encode, stream

import adapters


def allowance():
    # Synthetic values with the exact installed RunUsageAllowance shape.
    return {
        "tier": None,
        "window": {"used_percent": 0, "resets_at_ms": 1793141618000},
        "weekly": {"used_percent": 0, "resets_at_ms": 1793577600000},
        "as_of_ms": 1793125149015,
        "source": "stream",
    }


def selected():
    events, usage = stream()
    usage["payload"]["allowance"] = allowance()
    return events, usage


def test_typed_allowance_preserves_whole_usage_and_original_terminal_only():
    events, usage = selected()
    original = copy.deepcopy(usage)
    text = encode(events[:-1] + [usage, events[-1]])
    assert adapters.muse_terminal_text(text) == events[-1]["payload"]["text"]
    assert usage == original
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage])) == ""
    assert json.loads(text.splitlines()[-2]) == original


@pytest.mark.parametrize("period", ["window", "weekly"])
@pytest.mark.parametrize("value", [None, {"used_percent": 100, "resets_at_ms": None}])
def test_native_optional_period_and_unknown_reset_remain_data(period, value):
    events, usage = selected()
    usage["payload"]["allowance"][period] = value
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]]))


@pytest.mark.parametrize("tier", [None, "Pro", "Power", "x" * 128])
@pytest.mark.parametrize("source", ["stream", "mint"])
def test_bounded_native_allowance_tier_and_source_are_not_entitlement(tier, source):
    events, usage = selected()
    usage["payload"]["allowance"].update(tier=tier, source=source)
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]]))


@pytest.mark.parametrize(
    "path,value",
    [
        (("tier",), True),
        (("tier",), 1),
        (("tier",), ""),
        (("tier",), "   "),
        (("tier",), "x" * 129),
        (("source",), None),
        (("source",), True),
        (("source",), "unknown"),
        (("source",), "STREAM"),
        (("as_of_ms",), True),
        (("as_of_ms",), -1),
        (("as_of_ms",), 1.0),
        (("as_of_ms",), 2**63),
        (("window",), []),
        (("window",), 0),
        (("weekly",), "unknown"),
        (("window", "used_percent"), True),
        (("window", "used_percent"), -1),
        (("window", "used_percent"), 101),
        (("window", "used_percent"), 0.0),
        (("window", "used_percent"), "0"),
        (("weekly", "used_percent"), None),
        (("window", "resets_at_ms"), True),
        (("window", "resets_at_ms"), -1),
        (("window", "resets_at_ms"), 1.0),
        (("weekly", "resets_at_ms"), 2**63),
    ],
)
def test_allowance_exact_types_and_bounds(path, value):
    events, usage = selected()
    obj = usage["payload"]["allowance"]
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""


@pytest.mark.parametrize("field", ["tier", "window", "weekly", "as_of_ms", "source"])
def test_allowance_missing_field_not_ignored(field):
    events, usage = selected()
    del usage["payload"]["allowance"][field]
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""


@pytest.mark.parametrize("where", ["allowance", "window", "weekly"])
@pytest.mark.parametrize("field", ["unknown", "execution_authorized", "included_account"])
def test_allowance_extra_authority_or_unknown_fields_refused(where, field):
    events, usage = selected()
    obj = usage["payload"]["allowance"]
    if where != "allowance":
        obj = obj[where]
    obj[field] = True
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""


@pytest.mark.parametrize("period", ["window", "weekly"])
@pytest.mark.parametrize("field", ["used_percent", "resets_at_ms"])
def test_period_missing_field_refused(period, field):
    events, usage = selected()
    del usage["payload"]["allowance"][period][field]
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""


@pytest.mark.parametrize(
    "path,value",
    [
        (("stream", "id"), "foreign-session"),
        (("stream", "kind"), "session"),
        (("causation_id",), "foreign-command"),
        (("record_type",), "event"),
        (("durability",), "durable"),
        (("payload", "command_id"), "foreign-command"),
        (("payload", "run_stream", "id"), "foreign-run"),
        (("payload", "run_stream", "kind"), "session"),
    ],
)
def test_allowance_cannot_replace_exact_native_event_binding(path, value):
    events, usage = selected()
    obj = usage
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""


@pytest.mark.parametrize("position", [0, 1, 2, 4])
def test_allowance_not_terminal_or_prestart_permission(position):
    events, usage = selected()
    events.insert(position, usage)
    assert adapters.muse_terminal_text(encode(events)) == ""


def test_allowance_zero_percent_does_not_repair_failed_terminal_or_duplicate():
    events, usage = selected()
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1], events[-1]])) == ""
    events[-1]["payload"]["terminal"] = "failed"
    assert adapters.muse_terminal_text(encode(events[:-1] + [usage, events[-1]])) == ""
