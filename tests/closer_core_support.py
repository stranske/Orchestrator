"""Locate the independently pinned Workflows source for cross-repository conformance."""

import os
from pathlib import Path


def core_source() -> Path:
    configured = os.environ.get("CLOSER_CORE_SOURCE")
    source = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(__file__).resolve().parents[2] / "portable"
    )
    if configured and not (source / "scripts/closer_runtime.py").is_file():
        raise RuntimeError("Configured Workflows Closer Core source is missing")
    return source
