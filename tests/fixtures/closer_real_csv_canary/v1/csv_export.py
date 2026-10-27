"""Declared partial fixture for the controlled Closer E2E negative test.
This revision is intentionally incomplete; it is not production functionality.
"""

import json


def export_rows(rows, format):
    if format == "json":
        return json.dumps(rows)
    if format == "csv":
        raise NotImplementedError("Declared incomplete CSV canary fixture")
    raise ValueError("unsupported format")
