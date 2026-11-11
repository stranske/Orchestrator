"""Strict final-response JSON reader retained after Closer audit retirement."""

from __future__ import annotations

import argparse
import json
import math
from typing import Any


def strict_final(text: Any) -> dict[str, Any]:
    """One final JSON object; no event streams, prose, duplicate keys or NaN."""
    if not isinstance(text, str) or len(text.encode()) > 65536:
        raise ValueError("final response is absent or exceeds 64 KiB")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate final response key")
            result[key] = value
        return result

    def finite(token):
        value = float(token)
        if not math.isfinite(value):
            raise ValueError("nonfinite final response value")
        return value

    def refuse(_token):
        raise ValueError("nonfinite final response value")

    try:
        value = json.loads(
            text, object_pairs_hook=unique, parse_float=finite, parse_constant=refuse
        )
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid final response JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("final response must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        assert strict_final('{"classification":"uncertain"}') == {"classification": "uncertain"}
        try:
            strict_final('{"accept":true,"accept":false}')
        except ValueError:
            print(
                "transport_qualification selftest: valid final parsed; duplicate keys rejected (2 checks)"
            )
            return 0
        raise AssertionError("duplicate JSON accepted")
    parser.error("the old Closer qualification audit is retired; only --selftest remains")


if __name__ == "__main__":
    raise SystemExit(main())
