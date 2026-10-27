"""Actual campaign-join expression only; no native admission is issued."""

import ast
import hashlib
from pathlib import Path

import pytest

import closer_campaign as campaign
import closer_coding_publication_io as publication_io
import closer_correction_controller as ctl
import closer_queue as queue


def expression():
    tree = ast.parse(Path(publication_io.__file__).read_text())
    scope = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_tick_scope")
    mapping = next(
        n.value
        for n in ast.walk(scope)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "admission" for t in n.targets)
    )
    value = next(
        v
        for k, v in zip(mapping.keys, mapping.values)
        if isinstance(k, ast.Constant) and k.value == "campaign_sha256"
    )
    return compile(
        ast.fix_missing_locations(ast.Expression(value)), "<actual-campaign-join>", "eval"
    )


def actual(document):
    return eval(expression(), {**publication_io.__dict__, "document": document})


def test_actual_campaign_timestamp_domain_is_supported_without_changing_private_wire():
    document = {"created_at": campaign._now(), "updated_at": campaign._now(), "revision": 1}
    assert type(document["created_at"]) is float
    with pytest.raises(ValueError, match="private JSON scalar unsupported"):
        ctl.digest(document)  # Exact old expression is the causal failure.
    assert actual(document) == hashlib.sha256(queue._canonical_bytes(document)).hexdigest()


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_internal_campaign_time_still_refuses(timestamp):
    with pytest.raises(ValueError):
        actual({"created_at": timestamp, "updated_at": 1.25})


def test_unicode_and_changed_timestamp_use_exact_existing_queue_domain():
    document = {"owner": "source-é", "created_at": 1.125, "updated_at": 2.25}
    expected = hashlib.sha256(queue._canonical_bytes(document)).hexdigest()
    assert actual(document) == expected
    assert actual({**document, "updated_at": 2.5}) != expected
    assert actual({**document, "updated_at": 2}) != actual({**document, "updated_at": True})
