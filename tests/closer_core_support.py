"""Locate the independently pinned Workflows source for cross-repository conformance."""

import os
from pathlib import Path

from env_prereq import closer_core_source_path


def core_source() -> Path:
    source = closer_core_source_path()
    if (
        os.environ.get("CLOSER_CORE_SOURCE")
        and not (source / "scripts/closer_runtime.py").is_file()
    ):
        raise RuntimeError("Configured Workflows Closer Core source is missing")
    return source
