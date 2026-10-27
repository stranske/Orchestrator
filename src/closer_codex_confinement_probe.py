"""Fixed synthetic confinement probe. Executed only by reviewed native command/exec.

No real credential or controller file is opened. All targets are newly owned
synthetic probe files; arguments are source-derived, never model input.
"""

import errno
import json
import socket
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 4:
        return 78
    allowed, outside, nonce = sys.argv[1:]
    if not nonce.isascii() or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        return 78
    allowed, outside = Path(allowed), Path(outside)
    results = {}
    target = allowed / "allowed"
    try:
        target.write_bytes(b"synthetic-allow")
        results["workspace_read_write"] = target.read_bytes() == b"synthetic-allow"
        moved = allowed / "renamed"
        target.rename(moved)
        moved.unlink()
        results["workspace_rename_delete"] = True
    except OSError:
        results["workspace_read_write"] = False
        results["workspace_rename_delete"] = False

    def denied(name, action):
        try:
            action()
        except OSError as exc:
            results[name] = exc.errno in {errno.EACCES, errno.EPERM}
        else:
            results[name] = False

    denied("outside_read_denied", lambda: (outside / "read").read_bytes())
    denied("outside_create_denied", lambda: (outside / "create").write_bytes(b"synthetic"))
    denied("outside_rename_denied", lambda: (outside / "rename").rename(outside / "renamed"))
    denied("outside_delete_denied", lambda: (outside / "delete").unlink())
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        denied("network_bind_denied", lambda: sock.bind(("127.0.0.1", 0)))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        denied("network_connect_denied", lambda: sock.connect(("127.0.0.1", 9)))
    print(
        json.dumps(
            {"schema": "codex-fixed-confinement-probe/v1", "nonce": nonce, "checks": results},
            separators=(",", ":"),
        )
    )
    return 0 if all(results.values()) and len(results) == 8 else 78


if __name__ == "__main__":
    raise SystemExit(main())
