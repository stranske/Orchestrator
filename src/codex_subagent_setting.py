"""Read and conditionally update Codex's default subagent model through app-server."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import subprocess
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from codex_family_update import version_and_family

MODEL_KEY = "default_subagent_model"
EFFORT_KEY = "default_subagent_reasoning_effort"
DESKTOP_CODEX = Path(
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
)
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
NATIVE_VERSION = re.compile(r"sha256:[0-9a-f]{64}")
REQUEST_TIMEOUT = 8.0


class SettingError(Exception):
    """Public failure reason with no raw API response or file content."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Setting:
    model: str
    effort: str
    version: str


@dataclass(frozen=True)
class UpdateResult:
    status: str
    setting: Setting


@dataclass(frozen=True)
class _Snapshot:
    setting: Setting
    native_version: str
    document: dict[str, Any]
    raw: bytes


class _AppServer:
    def __init__(self, config: Path, executable: str | Path | None) -> None:
        binary = executable or (DESKTOP_CODEX if DESKTOP_CODEX.is_file() else shutil.which("codex"))
        if binary is None:
            raise SettingError("app_server_unavailable")
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
        }
        env["CODEX_HOME"] = str(config.parent)
        try:
            self.process = subprocess.Popen(
                [str(binary), "app-server", "--listen", "stdio://"],
                cwd=config.parent,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise SettingError("app_server_unavailable") from None
        self.buffer = b""
        self.request_id = 0

    def __enter__(self) -> _AppServer:
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "orchestrator-subagent-setting", "version": "1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized", "params": {}})
        except SettingError:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_error: object) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message).encode() + b"\n")
            self.process.stdin.flush()
        except OSError:
            raise SettingError("app_server_unavailable") from None

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        self._send({"id": self.request_id, "method": method, "params": params})
        assert self.process.stdout is not None
        deadline = time.monotonic() + REQUEST_TIMEOUT
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while (remaining := deadline - time.monotonic()) > 0:
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    try:
                        event = json.loads(line)
                    except (UnicodeError, ValueError):
                        raise SettingError("invalid_app_server_response") from None
                    if not isinstance(event, dict) or event.get("id") != self.request_id:
                        continue
                    error = event.get("error")
                    if isinstance(error, dict):
                        data = error.get("data")
                        if (
                            isinstance(data, dict)
                            and data.get("config_write_error_code") == "configVersionConflict"
                        ):
                            raise SettingError("owner_changed")
                        raise SettingError("app_server_rejected")
                    result = event.get("result")
                    if not isinstance(result, dict):
                        raise SettingError("invalid_app_server_response")
                    return result
                if not selector.select(remaining):
                    break
                try:
                    block = os.read(self.process.stdout.fileno(), 65536)
                except OSError:
                    raise SettingError("app_server_unavailable") from None
                if not block:
                    raise SettingError("app_server_unavailable")
                self.buffer += block
        raise SettingError("app_server_timeout")


def _check_config(config: Path) -> Path:
    try:
        if config.name != "config.toml" or config.is_symlink():
            raise SettingError("invalid_config_path")
        path = config.resolve(strict=True)
        if not path.is_file():
            raise SettingError("invalid_config_path")
    except OSError:
        raise SettingError("invalid_config_path") from None
    return path


def _bytes(path: Path) -> bytes:
    try:
        if path.is_symlink():
            raise SettingError("owner_changed")
        return path.read_bytes()
    except OSError:
        raise SettingError("config_unavailable") from None


def _target_layer(source: dict[str, Any], path: Path) -> bool:
    return (
        source.get("type") == "user"
        and source.get("file") == str(path)
        and not source.get("profile")
    )


def _read(server: _AppServer, path: Path) -> _Snapshot:
    raw = _bytes(path)
    result = server.request("config/read", {"includeLayers": True})
    if _bytes(path) != raw:
        raise SettingError("owner_changed")
    try:
        document = tomllib.loads(raw.decode())
        model = document["agents"][MODEL_KEY]
        effort = document["agents"][EFFORT_KEY]
        layers = [row for row in result["layers"] if _target_layer(row["name"], path)]
        if len(layers) != 1:
            raise SettingError("user_layer_unavailable")
        layer = layers[0]
        native_version = layer["version"]
        if not NATIVE_VERSION.fullmatch(native_version):
            raise SettingError("user_layer_unavailable")
        if not version_and_family(model) or effort not in EFFORTS:
            raise SettingError("unsupported_setting")
        for key, value in ((MODEL_KEY, model), (EFFORT_KEY, effort)):
            origin = result["origins"][f"agents.{key}"]
            if (
                not _target_layer(origin["name"], path)
                or origin["version"] != native_version
                or result["config"]["agents"][key] != value
                or layer["config"]["agents"][key] != value
            ):
                raise SettingError("setting_overridden")
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SettingError("setting_unavailable") from None
    digest = hashlib.sha256(os.fsencode(path) + b"\0" + raw).hexdigest()
    return _Snapshot(
        Setting(model, effort, f"{native_version}/{digest}"), native_version, document, raw
    )


def read_setting(config: Path, *, executable: str | Path | None = None) -> Setting:
    """Return only this file's effective scoped values and file-bound version."""
    path = _check_config(config)
    with _AppServer(path, executable) as server:
        return _read(server, path).setting


def update_setting(
    config: Path,
    *,
    expected_model: str,
    expected_effort: str,
    expected_version: str,
    successor: str,
    executable: str | Path | None = None,
) -> UpdateResult:
    """Make at most one native conditional write, with a verified readback."""
    current_identity = version_and_family(expected_model)
    successor_identity = version_and_family(successor)
    if current_identity is None or successor_identity is None:
        raise SettingError("unsupported_model")
    if current_identity[1] != successor_identity[1]:
        raise SettingError("different_model_family")
    path = _check_config(config)
    with _AppServer(path, executable) as server:
        before = _read(server, path)
        if before.setting != Setting(expected_model, expected_effort, expected_version):
            raise SettingError("owner_changed")
        if _bytes(path) != before.raw:
            raise SettingError("owner_changed")
        if before.setting.model == successor:
            return UpdateResult("unchanged", before.setting)
        try:
            result = server.request(
                "config/value/write",
                {
                    "filePath": str(path),
                    "expectedVersion": before.native_version,
                    "keyPath": f"agents.{MODEL_KEY}",
                    "mergeStrategy": "replace",
                    "value": successor,
                },
            )
        except SettingError as exc:
            if exc.reason == "owner_changed":
                raise
            raise SettingError("write_unconfirmed") from None
        try:
            after = _read(server, path)
        except SettingError:
            raise SettingError("write_unconfirmed") from None
        expected = {
            **before.document,
            "agents": {**before.document["agents"], MODEL_KEY: successor},
        }
        if (
            result.get("filePath") != str(path)
            or result.get("status") != "ok"
            or result.get("version") != after.native_version
            or after.document != expected
        ):
            raise SettingError("write_unconfirmed")
    return UpdateResult("updated", after.setting)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("read")
    update = commands.add_parser("set")
    update.add_argument("--expected-model", required=True)
    update.add_argument("--expected-effort", required=True)
    update.add_argument("--expected-version", required=True)
    update.add_argument("--successor", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "read":
            result = {"status": "read", **asdict(read_setting(args.config))}
        else:
            updated = update_setting(
                args.config,
                expected_model=args.expected_model,
                expected_effort=args.expected_effort,
                expected_version=args.expected_version,
                successor=args.successor,
            )
            result = {"status": updated.status, **asdict(updated.setting)}
    except SettingError as exc:
        print(json.dumps({"status": "blocked", "reason": exc.reason}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
