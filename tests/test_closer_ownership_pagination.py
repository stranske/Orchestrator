"""Smaller native pages preserve full replies and the original event/resource bounds."""

import copy
import time

import pytest

import closer_correction_ownership as ownership
import closer_dispatcher as closer

ENDPOINT = "repos/stranske/orchestrator/issues/984/timeline"


def reader(monkeypatch, rows, *, request_size=None):
    monkeypatch.setattr(closer.gh_capacity, "throttle", lambda *_a, **_k: {"action": "allow"})
    calls = []

    def api(args, **_kwargs):
        assert args[:3] == ["api", "--method", "GET"]
        path, query = args[3].split("?")
        assert path == ENDPOINT
        fields = dict(item.split("=") for item in query.split("&"))
        size, page = int(fields["per_page"]), int(fields["page"])
        if request_size is not None:
            assert size == request_size
        calls.append((size, page))
        return 0, copy.deepcopy(rows[(page - 1) * size : page * size])

    return (
        ownership._Reads(closer.GitHub(deadline=time.time() + 60, api_fn=api), time.time() + 60),
        calls,
    )


def events(count, *, body_size=0):
    return [
        {
            "id": n + 1,
            "event": "commented",
            "body": "x" * body_size,
            "unrecognized_native_field": {"retain": n},
        }
        for n in range(count)
    ]


def test_large_complete_reply_is_split_without_projection_or_omission(monkeypatch):
    original = events(29, body_size=5000)
    assert len(ownership._raw(original)) > 131072
    actual, calls = reader(monkeypatch, original, request_size=25)
    assert actual.pages(ENDPOINT) == original
    assert calls == [(25, 1), (25, 2)]
    assert actual.bytes == sum(len(ownership._raw(page)) for page in (original[:25], original[25:]))
    assert len(actual.refs) == 2 and all("per_page=25&page=" in ref for ref in actual.refs)
    assert ownership.MAX_REPLY_BYTES == 131072 and ownership.MAX_TOTAL_BYTES == 1048576
    assert ownership.MAX_CALLS == 32 and ownership.MAX_ITEMS == 400


def test_short_last_page_preserves_original_event_envelope(monkeypatch):
    original = events(399)
    actual, calls = reader(monkeypatch, original)
    assert actual.pages(ENDPOINT) == original
    assert len(calls) == 16 and calls[-1] == (25, 16)


def test_full_final_page_holds_without_inventing_completeness(monkeypatch):
    actual, calls = reader(monkeypatch, events(400))
    with pytest.raises(ownership.OwnershipUnavailable, match="pagination incomplete"):
        actual.pages(ENDPOINT)
    assert len(calls) == 16 and actual.calls == 16


def test_overlap_across_small_pages_still_holds(monkeypatch):
    original = events(26)
    original[-1] = copy.deepcopy(original[0])
    actual, _ = reader(monkeypatch, original)
    with pytest.raises(ownership.OwnershipUnavailable, match="pagination overlaps"):
        actual.pages(ENDPOINT)


@pytest.mark.parametrize("resource", ["reply", "total", "calls"])
def test_smaller_pages_do_not_bypass_original_resource_caps(monkeypatch, resource):
    original = events(26, body_size=6000 if resource == "reply" else 0)
    actual, calls = reader(monkeypatch, original)
    if resource == "total":
        actual.bytes = ownership.MAX_TOTAL_BYTES - 1
    elif resource == "calls":
        actual.calls = ownership.MAX_CALLS - 1
    with pytest.raises(ownership.OwnershipUnavailable, match="budget exhausted"):
        actual.pages(ENDPOINT)
    assert len(calls) == 1
    if resource == "reply":
        assert actual.bytes == len(ownership._raw(original[:25])) > ownership.MAX_REPLY_BYTES
        assert actual.refs == []


def test_unknown_connected_events_are_not_silently_omitted(monkeypatch):
    original = events(26)
    original[-1]["event"] = "connected"
    actual, _ = reader(monkeypatch, original)
    with pytest.raises(ownership.OwnershipUnavailable, match="connected lineage"):
        actual.pages(ENDPOINT)


@pytest.mark.parametrize("malformed", [{"not": "a native page"}, events(26)])
def test_malformed_or_oversized_native_page_is_counted_then_held(monkeypatch, malformed):
    actual, _ = reader(monkeypatch, [])
    actual.github.api_fn = lambda *_a, **_kw: (0, copy.deepcopy(malformed))
    with pytest.raises(ownership.OwnershipUnavailable, match="native page malformed"):
        actual.pages(ENDPOINT)
    assert actual.calls == 1 and actual.bytes == len(ownership._raw(malformed))
