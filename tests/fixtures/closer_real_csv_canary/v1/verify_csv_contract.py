"""Explicit standard-library contract script for the Closer CSV canary fixture.

Invoke this file directly. It is not a pytest module and defines no test items.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
import traceback
from pathlib import Path


def _load_export():
    path = Path(__file__).resolve().parent / "csv_export.py"
    spec = importlib.util.spec_from_file_location("closer_csv_canary_export", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _csv_text(module, rows):
    try:
        output = module.export_rows(rows, "csv")
    except NotImplementedError as exc:
        raise AssertionError("CSV contract is not implemented") from exc
    assert isinstance(output, str), "CSV export must return a string"
    return output


def _assert_crlf_records(text, record_count):
    """Record endings outside quoted fields are CRLF, never a bare CR or LF."""
    assert text.startswith("name,score\r\n"), repr(text[:32])
    seen = 0
    index = 0
    in_quotes = False
    length = len(text)
    while index < length:
        char = text[index]
        if in_quotes:
            if char == '"':
                if index + 1 < length and text[index + 1] == '"':
                    index += 2
                    continue
                in_quotes = False
            index += 1
            continue
        if char == '"':
            in_quotes = True
            index += 1
            continue
        if char in "\r\n":
            assert text.startswith(
                "\r\n", index
            ), f"record terminator is not CRLF at offset {index}"
            seen += 1
            index += 2
            continue
        index += 1
    assert not in_quotes, "unterminated quoted field"
    assert seen == record_count, f"expected {record_count} CRLF records, found {seen}"


def _assert_csv_round_trip(module, rows):
    text = _csv_text(module, rows)
    _assert_crlf_records(text, 1 + len(rows))
    parsed = list(csv.reader(io.StringIO(text, newline="")))
    assert parsed[0] == ["name", "score"], parsed[:1]
    expected = [[row["name"], str(row["score"])] for row in rows]
    assert parsed[1:] == expected, parsed[1:]
    assert len(parsed) == 1 + len(rows)


def _assert_json(module, rows):
    text = module.export_rows(rows, "json")
    assert text == json.dumps(rows), "JSON serialization changed"
    loaded = json.loads(text)
    assert len(loaded) == len(rows)
    for original, got in zip(rows, loaded, strict=True):
        assert got["name"] == original["name"]
        assert got["score"] == original["score"]


def _assert_unsupported(module, rows):
    try:
        module.export_rows(rows, "xml")
    except ValueError:
        return
    raise AssertionError("unsupported format did not raise ValueError")


def run_contract():
    module = _load_export()
    plain = [{"name": "alpha", "score": 1}, {"name": "beta", "score": 2}]
    _assert_json(module, plain)
    _assert_unsupported(module, plain)
    empty = _csv_text(module, [])
    assert empty == "name,score\r\n", repr(empty)
    _assert_csv_round_trip(module, plain)
    special = [
        {"name": "a,b", "score": "1"},
        {"name": 'say "hi"', "score": "2"},
        {"name": "line\nbreak", "score": "3"},
        {"name": "cr\rhere", "score": "4"},
        {"name": 'mix, "q"\r\nend', "score": "5\r\n,x"},
        {"name": 'a,b "c"\n\r', "score": "together"},
    ]
    _assert_csv_round_trip(module, special)


def main():
    try:
        run_contract()
    except AssertionError:
        traceback.print_exc()
        print("contract assertion failed", file=sys.stderr)
        return 1
    print("contract ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
