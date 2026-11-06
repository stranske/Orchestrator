"""CSV/JSON export fixture for the controlled Closer canary.

JSON preserves json.dumps round-trip behavior, including the fixed fields name
and score. CSV emits the header name,score, one record per input row, and CRLF
record endings. Unsupported formats raise ValueError.
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
        writer.writerow(["name", "score"])
        for row in rows:
            writer.writerow([row["name"], row["score"]])
        return buffer.getvalue()
    raise ValueError("unsupported format")
