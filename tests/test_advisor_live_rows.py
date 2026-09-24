"""The advisor offers LIVE rows only, on every path, and names the bound rows it will not offer.

`capabilities.NOT_LIVE_STATES` is the one definition of "not a live capability". The trigger loop and
the classification-miss path already applied it; two paths did not:

* the DIRECT-ENTRY branch appended the mapped capability with no status check, so a retired
  `offload` or `runtime-ac-checks` would still have been offered; and
* the classified path inserted only live bound rows but REPORTED the raw binding, so a retired bound
  row was counted in `bound_count` and listed in `bound_capabilities` while never being offered.

One live binding map now drives insertion, annotation and reporting on every branch. The bound rows
it leaves out are named rather than dropped: `bound_not_live` by status, and `bound_unregistered` for
a binding with no ledger row, so the surface's declared set is always accounted for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import capabilities
import capability_advisor as ca

SURFACE = "t-live-rows"
BINDING = {
    "bound-live": "live, so offered",
    "bound-retired": "retired, so never offered",
    "bound-superseded": "superseded, so never offered",
    "bound-ghost": "declared here, with no ledger row",
}
LEDGER = {
    "bound-live": "generated",
    "bound-retired": "retired",
    "bound-superseded": "superseded",
}
CLASSIFIES = "add unit tests for the retry helper"
MISSES = "xyzzy plugh frobnicate"


def _ledger(tmp_path: Path, statuses: dict[str, str]) -> Path:
    path = tmp_path / "capabilities.json"
    rows = {}
    for cap_id, status in statuses.items():
        row = capabilities._blank_capability(cap_id)
        row["status"] = status
        rows[cap_id] = row
    capabilities.save(rows, path)
    return path


def _ids(result: dict) -> list[str]:
    return [m["capability_id"] for m in result["capabilities"]]


@pytest.fixture
def bound_surface(monkeypatch):
    monkeypatch.setitem(ca.SURFACE_BINDINGS, SURFACE, dict(BINDING))
    return SURFACE


def test_a_retired_direct_entry_target_is_not_offered(tmp_path):
    # `runtime-ac-checks` is the live control: the same branch must still offer a live target, so
    # the retired assertion cannot pass by the branch never running at all.
    path = _ledger(tmp_path, {"offload": "retired", "runtime-ac-checks": "generated"})
    got = ca.advise("offload the acceptance criteria runtime check", record=False, path=path)
    assert {"offload", "runtime_ac"} <= set(got["task_types"]), got["task_types"]
    by_id = {m["capability_id"]: m for m in got["capabilities"]}
    assert by_id["runtime-ac-checks"].get("entered_directly") is True, by_id
    assert "offload" not in by_id, by_id["offload"]


@pytest.mark.parametrize("text", [CLASSIFIES, MISSES], ids=["classified", "classification-miss"])
def test_the_reported_bound_set_is_the_offered_bound_set(tmp_path, bound_surface, text):
    got = ca.advise(text, surface=bound_surface, record=False, path=_ledger(tmp_path, LEDGER))
    assert bool(got["task_types"]) == (text == CLASSIFIES), got["task_types"]
    assert got["bound_count"] == 1, got["bound_count"]
    assert got["bound_capabilities"] == ["bound-live"], got["bound_capabilities"]
    flagged = sorted(m["capability_id"] for m in got["capabilities"] if m.get("bound"))
    assert flagged == got["bound_capabilities"], flagged
    for entry in got["capabilities"]:
        assert ("binding_reason" in entry) == bool(entry.get("bound")), entry
    assert not {"bound-retired", "bound-superseded", "bound-ghost"} & set(_ids(got)), _ids(got)


@pytest.mark.parametrize("text", [CLASSIFIES, MISSES], ids=["classified", "classification-miss"])
def test_excluded_bound_rows_are_named_not_dropped(tmp_path, bound_surface, text):
    path = _ledger(tmp_path, LEDGER)
    got = ca.advise(text, surface=bound_surface, record=False, path=path)
    assert got["bound_not_live"] == {
        "retired": ["bound-retired"],
        "superseded": ["bound-superseded"],
    }, got["bound_not_live"]
    assert got["bound_unregistered"] == ["bound-ghost"], got["bound_unregistered"]
    # THE WHOLE DECLARED SET, derived from the resolver rather than restated, so the three fields
    # partition exactly what `binding_for` returns.
    named = [
        *got["bound_capabilities"],
        *(cap_id for ids in got["bound_not_live"].values() for cap_id in ids),
        *got["bound_unregistered"],
    ]
    assert sorted(named) == sorted(ca.binding_for(bound_surface, path=path)), named


def test_a_surface_bound_only_to_rows_it_cannot_offer_still_names_them(tmp_path, monkeypatch):
    monkeypatch.setitem(
        ca.SURFACE_BINDINGS,
        "t-all-gone",
        {"bound-retired": "retired", "bound-ghost": "no ledger row"},
    )
    got = ca.advise(MISSES, surface="t-all-gone", record=False, path=_ledger(tmp_path, LEDGER))
    assert got["capabilities"] == [] and got["bound_count"] == 0, got
    assert got["bound_capabilities"] == [], got["bound_capabilities"]
    assert got["bound_not_live"] == {"retired": ["bound-retired"]}, got["bound_not_live"]
    assert got["bound_unregistered"] == ["bound-ghost"], got["bound_unregistered"]


def test_a_suppressed_surface_carries_the_same_empty_fields(tmp_path):
    got = ca.advise(
        MISSES, surface="repo-audit:phase-1", record=False, path=_ledger(tmp_path, LEDGER)
    )
    assert got["confidence"] == "suppressed", got["confidence"]
    assert (got["bound_count"], got["bound_capabilities"]) == (0, []), got
    assert (got["bound_not_live"], got["bound_unregistered"]) == ({}, []), got


def test_the_text_answer_names_what_it_did_not_offer_and_is_quiet_otherwise(
    tmp_path, bound_surface, monkeypatch
):
    path = _ledger(tmp_path, LEDGER)
    text = ca.format_advice(ca.advise(CLASSIFIES, surface=bound_surface, record=False, path=path))
    line = next(ln for ln in text.splitlines() if "not offered" in ln)
    for cap_id in ("bound-retired", "bound-superseded", "bound-ghost"):
        assert cap_id in line, line
    monkeypatch.setitem(ca.SURFACE_BINDINGS, SURFACE, {"bound-live": "live, so offered"})
    quiet = ca.format_advice(ca.advise(CLASSIFIES, surface=bound_surface, record=False, path=path))
    assert "not offered" not in quiet, quiet
