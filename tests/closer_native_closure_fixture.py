"""Explicit synthetic GitHub ports in a genuine, source-command closure child.

Only the exact fixed closure argv is admitted. The OS child, bounded_run process
observation, supervisor actor, guards and outcome/operation writers stay real.
This fixture grants no production/provider qualification or closure authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

CLOSURE_CODE = "import sys,json;sys.path.insert(0,sys.argv[1]);import keepalive_supervisor as s;print(json.dumps(s.close_verified_issues(targets=[sys.argv[2]],limit=1)))"
TARGET = "owner/repo#12"
_CONFIG = "CLOSER_TEST_CLOSURE_CONFIG"
_DIGEST = "CLOSER_TEST_CLOSURE_CONFIG_SHA256"
MAX_BYTES = 128 * 1024


def sha(path):
    assert path.is_file() and not path.is_symlink()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_config(path, expected):
    assert path.is_absolute() and path.resolve(strict=True) == path
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.getuid() and 0 < info.st_size <= MAX_BYTES
        raw = stream.read(MAX_BYTES + 1)
    assert len(raw) <= MAX_BYTES and hashlib.sha256(raw).hexdigest() == expected
    return json.loads(raw)


def expected_command(source):
    return [sys.executable, "-c", CLOSURE_CODE, str(source), TARGET]


class NativeClosureFixture:
    def __init__(self, root, delivery, caller, patch, *, source):
        assert root.is_absolute() and root.resolve() == root
        assert delivery.root.resolve() == caller.world.root.resolve() == root.parent
        import closer_dispatcher

        assert source.resolve(strict=True) == Path(closer_dispatcher.__file__).resolve().parent
        self.root, self.delivery, self.caller = root, delivery, caller
        self.source = source.resolve(strict=True)
        self.children = []
        self.files = []
        self.root.mkdir(mode=0o700)
        assert self.root.stat().st_mode & 0o777 == 0o700
        self.site = self.root / "site"
        self.site.mkdir(mode=0o700)
        module = Path(__file__).resolve(strict=True)
        self.module = module
        self.module_sha = sha(module)
        self.source_pins = {str(p): sha(p) for p in self.source.glob("*.py")}
        startup = (
            "import os,sys\n"
            "try:\n"
            f" sys.path.insert(0,{str(module.parent)!r})\n"
            " from closer_native_closure_fixture import install_child_ports\n"
            " install_child_ports()\n"
            "except BaseException:\n"
            " os.write(2,b'private closure fixture startup refused\\n')\n"
            " os._exit(86)\n"
        )
        self.startup = self.site / "sitecustomize.py"
        self.startup.write_text(startup)
        self.startup.chmod(0o600)
        self.startup_sha = sha(self.startup)
        patch.setattr(subprocess, "Popen", self.popen)

    def popen(self, command, **kwargs):
        if list(command) != expected_command(self.source):
            return self.caller.popen(command, **kwargs)
        assert kwargs == {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "start_new_session": True,
        }
        assert sha(self.module) == self.module_sha and sha(self.startup) == self.startup_sha
        assert all(sha(Path(p)) == value for p, value in self.source_pins.items())
        d = self.delivery
        private = self.root / f"child-{len(self.children) + 1}"
        private.mkdir(mode=0o700)
        config = private / "config.json"
        payload = {
            "fixture_root": str(d.root.resolve()),
            "source": str(self.source),
            "source_pins": self.source_pins,
            "helper": str(self.module),
            "helper_sha256": self.module_sha,
            "target": TARGET,
            "pr": d.closure_options["pr_read"](TARGET),
            "issue": copy.deepcopy(d.source_issue),
            "ledger": str(d.root / "capabilities.json"),
            "brain": str(d.brain),
            "state": str(d.root / "supervisor"),
            "observed": str(private / "observed.json"),
        }
        raw = json.dumps(payload, sort_keys=True, allow_nan=False).encode()
        assert len(raw) <= MAX_BYTES
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        env = {
            "HOME": str(d.root / "private-home"),
            "HANDOFF_DIR": str(d.root / "handoff"),
            "ORCH_STATE_DIR": payload["state"],
            "ORCH_SUPERVISOR_CLOSE": "1",
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(self.site),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            _CONFIG: str(config),
            _DIGEST: hashlib.sha256(raw).hexdigest(),
        }
        # Actual Popen; original source argv is unchanged, no fake PID/process.
        from closer_inert_dispatcher_support import REAL_POPEN

        child = REAL_POPEN(command, env=env, **kwargs)
        self.caller.children.add(child.pid)
        self.children.append(child)
        self.files.append(private / "observed.json")
        return child


def install_child_ports():
    """Site startup permits only fixture data I/O before the actual actor runs."""
    config = read_config(Path(os.environ[_CONFIG]), os.environ[_DIGEST])
    assert sys.argv == ["-c", config["source"], TARGET]
    assert config["target"] == TARGET
    assert Path(config["helper"]).resolve() == Path(__file__).resolve()
    assert sha(Path(__file__)) == config["helper_sha256"]
    root = Path(config["fixture_root"])
    assert root.resolve(strict=True) == root and root.stat().st_uid == os.getuid()
    assert root.stat().st_mode & 0o777 == 0o700
    assert Path(os.environ[_CONFIG]).is_relative_to(root / "native-closure")
    for key, relative in (
        ("ledger", "capabilities.json"),
        ("brain", "brain.db"),
        ("state", "supervisor"),
    ):
        assert Path(config[key]) == root / relative
        assert Path(config[key]).resolve() == Path(config[key])
    observed_path = Path(config["observed"])
    assert observed_path.parent == Path(os.environ[_CONFIG]).parent
    assert observed_path.name == "observed.json" and not observed_path.is_symlink()
    source = Path(config["source"])
    assert source.resolve(strict=True) == source
    assert set(config["source_pins"]) == {str(p) for p in source.glob("*.py")}
    assert all(sha(Path(p)) == value for p, value in config["source_pins"].items())
    sys.path.insert(0, str(source))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network/subprocess forbidden in private closure child")

    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden
    socket.getaddrinfo = forbidden
    subprocess.Popen = forbidden
    subprocess.run = forbidden
    import backlog
    import capabilities
    import feedback
    import keepalive_supervisor as actor

    assert Path(actor.__file__).resolve() == source / "keepalive_supervisor.py"
    backlog.SUPPORTED_REPOS = ["owner/repo"]  # Synthetic fixture repository configuration.
    capabilities.REG = Path(config["ledger"])
    feedback.DB_PATH = Path(config["brain"])
    observed = Path(config["observed"])
    issue, pr, calls = config["issue"], config["pr"], []

    def persist():
        observed.write_text(json.dumps({"issue": issue, "pr": pr, "calls": calls}))

    def gh(args, *, pages=False):
        calls.append(args)
        issue_api = "repos/owner/repo/issues/2"
        if args == ["api", issue_api]:
            result = copy.deepcopy(issue)
        elif args == ["api", issue_api, "--method", "PATCH", "-f", "state=closed"]:
            issue["state"] = "closed"
            result = copy.deepcopy(issue)
        elif args == ["api", issue_api + "/comments", "--paginate", "--slurp"]:
            result = [[]]
        elif (
            len(args) == 4
            and args[:3]
            in (
                ["api", issue_api + "/comments", "-f"],
                ["api", "repos/owner/repo/issues/12/comments", "-f"],
            )
            and args[3].startswith("body=<!--")
        ):
            pr["comments"]["nodes"].append(
                {"body": args[3][5:], "author": {"login": "fixture-owner"}}
            )
            result = {}
        else:
            raise AssertionError("unrecognized private fixture GitHub API argv")
        persist()
        return result

    def read_pr(target):
        assert target == TARGET
        return copy.deepcopy(pr)

    actor._closure_gh = gh
    actor._closure_pr = read_pr
    persist()
