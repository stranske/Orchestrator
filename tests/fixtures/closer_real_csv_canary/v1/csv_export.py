"""Isolated CSV/JSON canary fixture for the controlled Closer corrective test.

JSON export is ``json.dumps`` of the input rows. CSV export writes the fixed
fields ``name`` and ``score`` with CRLF record endings, one record per input.
Empty input returns the header alone. This module is fixture code, not
production functionality.
"""

import csv
import io
import json


def export_rows(rows, format):
    if format == "json":
        return json.dumps(rows)
    if format == "csv":
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(("name", "score"))
        for row in rows:
            writer.writerow((row["name"], row["score"]))
        return buffer.getvalue()
    raise ValueError("unsupported format")
