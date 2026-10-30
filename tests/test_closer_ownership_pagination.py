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
    actual, calls = reader(monkeypatch, original, request_size=15)
    assert actual.pages(ENDPOINT) == original
    assert calls == [(15, 1), (15, 2)]
    assert actual.bytes == sum(len(ownership._raw(page)) for page in (original[:15], original[15:]))
    assert len(actual.refs) == 2 and all("per_page=15&page=" in ref for ref in actual.refs)
    assert ownership.MAX_REPLY_BYTES == 131072 and ownership.MAX_TOTAL_BYTES == 1048576
    assert ownership.MAX_CALLS == 32 and ownership.MAX_ITEMS == 400


def test_short_last_page_preserves_original_event_envelope(monkeypatch):
    original = events(400)
    actual, calls = reader(monkeypatch, original)
    assert actual.pages(ENDPOINT) == original
    assert len(calls) == 27 and calls[-1] == (15, 27)


@pytest.mark.parametrize("count", [401, 404, 405])
def test_short_or_full_final_page_cannot_exceed_the_item_cap(monkeypatch, count):
    actual, calls = reader(monkeypatch, events(count))
    with pytest.raises(ownership.OwnershipUnavailable, match="item budget exhausted"):
        actual.pages(ENDPOINT)
    assert len(calls) == 27 and actual.calls == 27


def test_page_walk_never_overshoots_the_shared_call_budget(monkeypatch):
    actual, calls = reader(monkeypatch, events(400))
    actual.calls = ownership.MAX_CALLS - 5
    with pytest.raises(ownership.OwnershipUnavailable, match="call budget exhausted"):
        actual.pages(ENDPOINT)
    assert len(calls) == 5 and actual.calls == ownership.MAX_CALLS


def test_overlap_across_small_pages_still_holds(monkeypatch):
    original = events(26)
    original[-1] = copy.deepcopy(original[0])
    actual, _ = reader(monkeypatch, original)
    with pytest.raises(ownership.OwnershipUnavailable, match="pagination overlaps"):
        actual.pages(ENDPOINT)


@pytest.mark.parametrize("resource", ["reply", "total", "calls"])
def test_smaller_pages_do_not_bypass_original_resource_caps(monkeypatch, resource):
    original = events(26, body_size=9000 if resource == "reply" else 0)
    actual, calls = reader(monkeypatch, original)
    if resource == "total":
        actual.bytes = ownership.MAX_TOTAL_BYTES - 1
    elif resource == "calls":
        actual.calls = ownership.MAX_CALLS - 1
    with pytest.raises(ownership.OwnershipUnavailable, match="budget exhausted"):
        actual.pages(ENDPOINT)
    assert len(calls) == 1
    if resource == "reply":
        assert actual.bytes == len(ownership._raw(original[:15])) > ownership.MAX_REPLY_BYTES
        assert actual.refs == []


def test_single_oversize_event_still_refuses(monkeypatch):
    original = events(1, body_size=ownership.MAX_REPLY_BYTES)
    actual, calls = reader(monkeypatch, original)
    with pytest.raises(ownership.OwnershipUnavailable, match="response budget exhausted"):
        actual.pages(ENDPOINT)
    assert calls == [(15, 1)]
    assert actual.bytes == len(ownership._raw(original)) > ownership.MAX_REPLY_BYTES
    assert actual.refs == []


def test_two_current_sized_native_timelines_retain_each_raw_event(monkeypatch):
    target_endpoint = ENDPOINT
    source_endpoint = "repos/stranske/orchestrator/issues/983/timeline"
    raw_pages = {
        target_endpoint: events(35, body_size=6000),
        source_endpoint: events(23, body_size=6000),
    }
    monkeypatch.setattr(closer.gh_capacity, "throttle", lambda *_a, **_k: {"action": "allow"})
    calls = []

    def api(args, **_kwargs):
        assert args[:3] == ["api", "--method", "GET"]
        path, query = args[3].split("?")
        fields = dict(item.split("=") for item in query.split("&"))
        size, page = int(fields["per_page"]), int(fields["page"])
        assert path in raw_pages and size == 15
        calls.append((path, page))
        rows = raw_pages[path]
        return 0, copy.deepcopy(rows[(page - 1) * size : page * size])

    actual = ownership._Reads(
        closer.GitHub(deadline=time.time() + 60, api_fn=api), time.time() + 60
    )
    assert actual.pages(target_endpoint) == raw_pages[target_endpoint]
    assert actual.pages(source_endpoint) == raw_pages[source_endpoint]
    assert calls == [
        (target_endpoint, 1),
        (target_endpoint, 2),
        (target_endpoint, 3),
        (source_endpoint, 1),
        (source_endpoint, 2),
    ]
    page_bytes = [
        len(ownership._raw(rows[index : index + ownership.PAGE_SIZE]))
        for rows in raw_pages.values()
        for index in range(0, len(rows), ownership.PAGE_SIZE)
    ]
    assert actual.calls == 5
    assert actual.bytes == sum(page_bytes) < ownership.MAX_TOTAL_BYTES
    assert max(page_bytes) <= ownership.MAX_REPLY_BYTES


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
