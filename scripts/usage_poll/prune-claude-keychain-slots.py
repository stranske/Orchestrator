#!/usr/bin/env python3
"""Prune dormant Claude credential slots using metadata only; dry-run by default.

Based on ~/.codex/bin/prune-claude-keychain-slots.py. Always keep the default
slot, the poller slot, unknown timestamps, and anything named in --keep.
--apply deletes only slots untouched for strictly more than --days (at least 30).
A deleted slot's config directory must log in again. Never request token values.
"""

import argparse
import datetime
import hashlib
import pathlib
import re
import subprocess
import sys


def parse_slots(dump):
    """Read service and modification metadata from each independent item block.

    security dump-keychain places mdat BEFORE svce; searching the whole block
    avoids associating one item's timestamp with the next item's service.
    """
    out = []
    for block in re.split(r"(?m)^keychain: ", dump):
        service = re.search(r'"svce"<blob>="(Claude Code-credentials(?:-([0-9a-f]{8}))?)"', block)
        if not service:
            continue
        modified = re.search(r'"mdat"<timedate>=0x[0-9A-Fa-f]+\s+"(\d{14})Z', block)
        timestamp = None
        if modified:
            try:
                timestamp = datetime.datetime.strptime(modified.group(1), "%Y%m%d%H%M%S").replace(
                    tzinfo=datetime.UTC
                )
            except ValueError:
                pass  # Unknown or malformed metadata must never authorize deletion.
        out.append((service.group(1), service.group(2), timestamp))
    return out


def slots(home, runner):
    result = runner(
        ["security", "dump-keychain", str(home / "Library/Keychains/login.keychain-db")],
        capture_output=True,
        text=True,
        check=True,
    )
    return parse_slots(result.stdout)


def select_slots(all_slots, *, now, poller, days=30, keep=()):
    """Pure selection: preserve protected slots and timestamps at/after cutoff."""
    if days < 30:
        raise ValueError("--days must be at least 30")
    protected = {poller, *keep}
    cutoff = now - datetime.timedelta(days=days)
    doomed, kept = [], []
    for name, suffix, timestamp in all_slots:
        age = (now - timestamp).days if timestamp else None
        if suffix is None or suffix in protected or timestamp is None or timestamp >= cutoff:
            kept.append((name, age))
        else:
            doomed.append((name, age))
    return doomed, kept


def main(argv=None, *, runner=None, home=None, now=None):
    runner = subprocess.run if runner is None else runner
    home = pathlib.Path.home() if home is None else pathlib.Path(home)
    now = datetime.datetime.now(datetime.UTC) if now is None else now
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--keep", action="append", default=[], help="extra 8-hex slot suffix to keep"
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.days < 30:
        parser.error("--days must be at least 30")
    poller = hashlib.sha256(str(home / ".codex/claude-poll").encode()).hexdigest()[:8]
    try:
        all_slots = slots(home, runner)
    except subprocess.CalledProcessError:
        print("Could not read keychain metadata; nothing deleted.", file=sys.stderr)
        return 1
    doomed, kept = select_slots(all_slots, now=now, poller=poller, days=args.days, keep=args.keep)
    print(
        f"slots: {len(all_slots)} | keep: {len(kept)} | prune: {len(doomed)} "
        f"(untouched > {args.days} days)"
    )
    for name, age in kept:
        print(f"  KEEP  {name}  (last written {age if age is not None else '?'} days ago)")
    for name, age in doomed[:5]:
        print(f"  prune {name}  ({age} days)")
    if len(doomed) > 5:
        print(f"  ... and {len(doomed) - 5} more (oldest {max(x[1] for x in doomed)} days)")
    if not args.apply:
        print("DRY RUN: nothing deleted. Re-run with --apply to delete the pruned slots.")
        return 0
    failed = 0
    for name, _ in doomed:
        result = runner(
            ["security", "delete-generic-password", "-s", name], capture_output=True, text=True
        )
        failed += result.returncode != 0
    print(f"deleted {len(doomed) - failed}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
