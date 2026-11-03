"""Exact explicit plain heading inventory; no source text or Tasks/AC rewrite."""

import hashlib
from pathlib import Path

import pytest

import closer_dispatcher as closer

BODY = Path(__file__).parent / "fixtures/sonnet_role_admission/original-983-body.txt"


def test_actual_unchanged_issue983_body_retains_all_six_original_clauses():
    raw = BODY.read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "64a22ce25c538cfeb98c058fc66bca2165ac829df1c037e5b95cefa22959646c"
    )
    text = raw.decode()
    expected = [line[2:] for line in text.splitlines() if line.startswith("- ")]
    assert len(expected) == 6
    assert closer.requirements(text) == expected
    assert BODY.read_bytes() == raw


@pytest.mark.parametrize(
    "heading",
    [
        "Tasks and acceptance criteria:",
        "tasks AND Acceptance CRITERIA:",
        "  Tasks \tand acceptance\tcriteria : \t",
    ],
)
def test_exact_case_and_ordinary_spacing_open_only_explicit_scope(heading):
    assert closer.requirements(heading + "\n- [ ] Preserve every item\nNormative prose\n") == [
        "Preserve every item",
        "Normative prose",
    ]


def test_plain_multiline_prose_bullets_order_duplicates_and_markdown_endsection():
    text = "Tasks and acceptance criteria:\nFirst normative line\ncontinuation line\n\n- [ ] repeat\n- [x] repeat\n1. last\n## Notes\nNot an acceptance criterion\n"
    assert closer.requirements(text) == [
        "First normative line",
        "continuation line",
        "repeat",
        "repeat",
        "last",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Tasks:\n- no",
        "Acceptance criteria:\n- no",
        "The message says Tasks and acceptance criteria:\n- no",
        '"Tasks and acceptance criteria:"\n- no',
        "> Tasks and acceptance criteria:\n> - no",
        "    Tasks and acceptance criteria:\n- no",
        "Tasks and acceptance criteria: this is incidental prose\n- no",
    ],
)
def test_unknown_quoted_incidental_or_indented_heading_does_not_authorize_inventory(text):
    assert closer.requirements(text) == []


@pytest.mark.parametrize("fence", ["```", "````", "~~~"])
def test_plain_heading_inside_fenced_code_does_not_open_scope(fence):
    text = (
        fence
        + "text\nTasks and acceptance criteria:\n- fake\n"
        + fence
        + "\n- still outside\nTasks and acceptance criteria:\n- real\n"
    )
    assert closer.requirements(text) == ["real"]


def test_short_or_foreign_fence_does_not_close_original_block():
    text = "````\n```\nTasks and acceptance criteria:\n- fake\n~~~\nTasks and acceptance criteria:\n- still fake\n````\nTasks and acceptance criteria:\n- real"
    assert closer.requirements(text) == ["real"]


def test_existing_markdown_sections_and_prose_are_unchanged():
    text = "## Tasks\nRequired prose\n- [ ] alpha\n- [x] alpha\nTasks and acceptance criteria:\n### Notes\nignore\n## Acceptance criteria\n1) beta\n"
    assert closer.requirements(text) == [
        "Required prose",
        "alpha",
        "alpha",
        "Tasks and acceptance criteria:",
        "beta",
    ]
