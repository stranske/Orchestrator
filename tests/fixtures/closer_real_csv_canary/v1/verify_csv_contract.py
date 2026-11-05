#!/usr/bin/env python3
"""Explicit standard-library contract for the isolated CSV canary fixture.

Invoke this script directly. It is not a production test: the module sets
``__test__ = False`` and defines no ``test_*`` functions, so pytest does not
collect it. Pass a ``csv_export.py`` path to check a specific revision;
the default is the sibling module.

Exit 0 when every check passes, 1 when the loaded ``export_rows`` fails the
contract, and 2 when the module cannot be loaded.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
from pathlib import Path

__test__ = False

_JSON_ROWS = [
    {"name": "ada", "score": 10, "extra": "kept"},
    {"name": "café", "score": 2.5},
]
_SIMPLE_ROWS = [
    {"name": "ada", "score": 1},
    {"name": "grace", "score": 2},
]
_SPECIAL_ROWS = [
    {"name": "a,b", "score": "1"},
    {"name": 'say "hi"', "score": "2"},
    {"name": "line\nbreak", "score": "3"},
    {"name": "cr\rhere", "score": "4"},
    {"name": "both\r\n", "score": 'quote, "and" comma'},
]


def _load_export_rows(path: Path):
    spec = importlib.util.spec_from_file_location("csv_canary_under_contract", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load export module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    export_rows = getattr(module, "export_rows", None)
    if not callable(export_rows):
        raise RuntimeError(f"{path} does not define export_rows")
    return export_rows


def _crlf_record_count(text: str) -> int:
    """Count CRLF record endings outside quoted fields.

    A bare CR or LF outside quotes is a contract failure. Escaped quotes
    (``""``) stay inside the quoted field and do not toggle quote state.
    """

    in_quotes = False
    index = 0
    records = 0
    while index < len(text):
        character = text[index]
        if character == '"':
            if in_quotes and index + 1 < len(text) and text[index + 1] == '"':
                index += 2
                continue
            in_quotes = not in_quotes
            index += 1
            continue
        if not in_quotes and character in "\r\n":
            if text.startswith("\r\n", index):
                records += 1
                index += 2
                continue
            raise AssertionError(
                f"record ending is not CRLF at offset {index}: {text[index : index + 4]!r}"
            )
        index += 1
    if in_quotes:
        raise AssertionError("CSV ended inside a quoted field")
    return records


def _parsed_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


def _check_json(export_rows) -> None:
    encoded = export_rows(_JSON_ROWS, "json")
    if encoded != json.dumps(_JSON_ROWS):
        raise AssertionError(f"JSON encoding diverged from json.dumps: {encoded!r}")
    if json.loads(encoded) != _JSON_ROWS:
        raise AssertionError("JSON round-trip did not restore the input rows")
    if export_rows([], "json") != "[]":
        raise AssertionError("empty JSON input did not round-trip as []")


def _check_unsupported(export_rows) -> None:
    for fmt in ("xml", "CSV", "", "tsv"):
        try:
            export_rows([], fmt)
        except ValueError:
            continue
        except Exception as exc:
            raise AssertionError(
                f"format {fmt!r} raised {type(exc).__name__}, expected ValueError"
            ) from exc
        else:
            raise AssertionError(f"format {fmt!r} did not raise ValueError")


def _check_csv(export_rows) -> None:
    empty = export_rows([], "csv")
    if empty != "name,score\r\n":
        raise AssertionError(f"empty CSV must be the header alone, got {empty!r}")

    simple = export_rows(_SIMPLE_ROWS, "csv")
    expected_simple = "name,score\r\nada,1\r\ngrace,2\r\n"
    if simple != expected_simple:
        raise AssertionError(f"simple CSV mismatch: {simple!r}")
    if _crlf_record_count(simple) != 1 + len(_SIMPLE_ROWS):
        raise AssertionError("simple CSV record count mismatch")

    special = export_rows(_SPECIAL_ROWS, "csv")
    if not special.startswith("name,score\r\n"):
        raise AssertionError(f"CSV header is not name,score with CRLF: {special[:40]!r}")
    if _crlf_record_count(special) != 1 + len(_SPECIAL_ROWS):
        raise AssertionError("special CSV did not emit one CRLF record per input row")
    parsed = _parsed_rows(special)
    if parsed[0] != ["name", "score"]:
        raise AssertionError(f"parsed header {parsed[0]!r}")
    body = parsed[1:]
    if len(body) != len(_SPECIAL_ROWS):
        raise AssertionError(f"parsed {len(body)} records from {len(_SPECIAL_ROWS)} inputs")
    for got, row in zip(body, _SPECIAL_ROWS, strict=True):
        expected = [row["name"], row["score"]]
        if got != expected:
            raise AssertionError(f"csv.reader round-trip {got!r} != {expected!r}")


def run_contract(export_rows) -> list[tuple[str, str | None]]:
    """Return ``(name, detail)`` per check. ``detail`` is None when the check passed."""

    checks = (
        ("json-round-trip", _check_json),
        ("unsupported-format", _check_unsupported),
        ("csv-shape-and-round-trip", _check_csv),
    )
    results = []
    for name, check in checks:
        try:
            check(export_rows)
        except Exception as exc:
            results.append((name, f"{type(exc).__name__}: {exc}"))
        else:
            results.append((name, None))
    return results


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] in {"-h", "--help"}):
        print(f"usage: {Path(argv[0]).name} [csv_export.py]", file=sys.stderr)
        return 2
    path = Path(argv[1]) if len(argv) == 2 else Path(__file__).with_name("csv_export.py")
    try:
        export_rows = _load_export_rows(path)
    except Exception as exc:
        print(f"LOAD FAIL {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    results = run_contract(export_rows)
    failed = 0
    for name, detail in results:
        if detail is None:
            print(f"PASS {name}")
            continue
        failed += 1
        print(f"FAIL {name}: {detail}")
    if failed:
        print(f"{failed} failed")
        return 1
    print(f"PASS {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
