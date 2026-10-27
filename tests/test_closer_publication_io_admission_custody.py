"""Real private custody round trips; no source/native admission is issued."""

import copy

import pytest
from test_closer_coding_publication import custody as custody

import closer_coding_publication as pub
import closer_coding_publication_io as io
import closer_correction_controller as ctl
import closer_queue as queue


def envelope():
    return {
        "record": {
            "binding": {"mechanism": "native-handoff/v1", "policy_digest": "b" * 64},
            "context": {
                "schema": io.ADMISSION_SCHEMA,
                "purpose": io.ACTION,
                "issued_at": 1.125,
                "expires_at": 121.125,
                "request_timeout_seconds": 120.0,
                "path": "/source-é",
            },
        },
        "signature": "a" * 64,
    }


def test_real_private_custody_preserves_exact_finite_float_signed_bytes(custody):
    signed = envelope()
    before = queue._canonical_bytes(signed)
    with pytest.raises(ValueError, match="private JSON scalar unsupported"):
        ctl.canonical(signed)  # Exact frozen predecessor storage failure.
    record = io.admission_custody_data(signed)
    sha = pub.digest(record)
    name = "publication-io-admission-" + sha + ".json"
    assert custody.create(name, record)
    actual = custody.read(name)
    assert pub.digest(actual) == sha
    decoded = io.read_admission_custody_data(actual)
    assert queue._canonical_bytes(decoded) == before
    assert decoded["signature"] == signed["signature"]
    assert type(decoded["record"]["context"]["issued_at"]) is float
    assert not custody.create(name, record)  # No replacement/reissuance.


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_clocks_remain_refused(value):
    signed = envelope()
    signed["record"]["context"]["issued_at"] = value
    with pytest.raises(ValueError):
        io.admission_custody_data(signed)


@pytest.mark.parametrize(
    "mutation", ["whitespace", "duplicate", "extra", "wrong_mode", "oversized"]
)
def test_noncanonical_or_foreign_storage_refuses(mutation):
    record = io.admission_custody_data(envelope())
    if mutation == "whitespace":
        record["signed_json"] = " " + record["signed_json"]
    elif mutation == "duplicate":
        record["signed_json"] = record["signed_json"].replace(
            '"issued_at":1.125', '"issued_at":0,"issued_at":1.125'
        )
    elif mutation == "extra":
        record["extra"] = False
    elif mutation == "oversized":
        record["signed_json"] = "x" * 65537
    else:
        signed = envelope()
        signed["record"]["context"]["purpose"] = "merge"
        record["signed_json"] = queue._canonical_bytes(signed).decode()
    with pytest.raises(ValueError):
        io.read_admission_custody_data(record)


def test_data_does_not_verify_or_issue_a_signature():
    signed = envelope()
    changed = copy.deepcopy(signed)
    changed["signature"] = "f" * 64
    first = io.admission_custody_data(signed)
    second = io.admission_custody_data(changed)
    assert pub.digest(first) != pub.digest(second)
    assert io.read_admission_custody_data(second)["signature"] == "f" * 64
    # These DATA helpers never claim that either synthetic HMAC is genuine.
