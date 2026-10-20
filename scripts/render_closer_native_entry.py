#!/usr/bin/env python3
"""Export an opt-in Cursor wrapper preview; never install or enable it."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from closer_native_observer import render_cursor_wrapper  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--campaign-root", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    source, output = Path(args.source).resolve(), Path(args.out)
    live = Path.home() / ".codex/bin/cursor-lane.sh"
    if output.is_symlink() or output.resolve() in {source, live.resolve()} or output.exists():
        parser.error("preview output must be a new file distinct from the native wrapper")
    manifest = json.loads(Path(args.manifest).read_text())
    rendered = render_cursor_wrapper(
        source.read_text(),
        manifest_path=Path(args.manifest).resolve(),
        campaign_root=Path(args.campaign_root).resolve(),
        interpreter=Path(manifest["runtime"]["python"]).resolve(),
        launcher=Path(manifest["module_path"]).parent / "closer_launcher.py",
    )
    with output.open("x") as stream:
        stream.write(rendered)
    print(
        json.dumps(
            {
                "kind": "native-wrapper-preview",
                "installed": False,
                "source": str(source),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "preview": str(output.resolve()),
                "preview_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "scheduler_origin": "unobserved",
                "desktop_qualified": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
