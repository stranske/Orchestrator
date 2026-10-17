"""The certifier's criterion parser reads the fleet's real PR bodies.

Measured 2026-10-17 on the newest 40 verifier disputes: 8 parsed, 27 were refused only
because the PR-meta workflow appends a ``**Head SHA:**`` status block after the
acceptance list, 1 used a numbered checklist, 4 had no acceptance heading. The first
three shapes are deterministic Markdown; the fourth stays refused.
"""

from __future__ import annotations

import pytest

import adjudicator_retro as retro

STATUS_BLOCK = (
    "## Summary\n\nDoes the thing.\n\n"
    "#### Acceptance criteria\n"
    "- [ ] Named test `tests/test_x.py::test_a` passes.\n"
    "- [x] `python3 -m pytest tests/test_x.py -q` exits 0.\n"
    "\n"
    "**Head SHA:** ba94d2de58f4f277d3ae061a45bac79cf0638552\n"
    "**Latest Runs:** queued Gate\n"
    "\n"
    "| Workflow / Job | Result | Logs |\n"
    "|---|---|---|\n"
    "| Gate | queued | - |\n"
)


def test_status_block_after_the_list_is_not_a_criterion():
    assert retro._criterion_texts(STATUS_BLOCK) == [
        "Named test `tests/test_x.py::test_a` passes.",
        "`python3 -m pytest tests/test_x.py -q` exits 0.",
    ]


def test_trailing_note_after_a_blank_line_is_not_a_criterion():
    body = (
        "## Acceptance Criteria\n- [ ] one\n- [ ] two\n\n"
        "These criteria restate the delivered change and validation above.\n"
    )
    assert retro._criterion_texts(body) == ["one", "two"]


def test_numbered_checklist_items_are_items():
    body = "## Acceptance Criteria\n1. [ ] first\n2. [ ] second\n3) third\n"
    assert retro._criterion_texts(body) == ["first", "second", "third"]


def test_items_resuming_after_prose_are_refused():
    body = "## Acceptance Criteria\n- [ ] one\n\nA note.\n\n- [ ] late item\n"
    with pytest.raises(ValueError, match="resume after prose"):
        retro._criterion_texts(body)


def test_prose_inside_the_list_is_still_refused():
    body = "## Acceptance Criteria\n- [ ] one\ncontinued on the next line\n- [ ] two\n"
    with pytest.raises(ValueError, match="multiline"):
        retro._criterion_texts(body)


def test_prose_before_the_first_item_is_still_refused():
    body = "## Acceptance Criteria\n\nThe following must hold:\n\n- [ ] one\n"
    with pytest.raises(ValueError, match="multiline"):
        retro._criterion_texts(body)


def test_other_headings_do_not_open_the_contract():
    body = "## Tasks and acceptance\n- [ ] one\n\n**Head SHA:** abc\n"
    with pytest.raises(ValueError, match="no explicit acceptance criteria"):
        retro._criterion_texts(body)


def test_next_heading_still_closes_the_section():
    body = "## Acceptance Criteria\n- [ ] one\n\n**Head SHA:** abc\n## Notes\n- not a criterion\n"
    assert retro._criterion_texts(body) == ["one"]
