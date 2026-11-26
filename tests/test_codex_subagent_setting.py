from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

import pytest

import codex_subagent_setting as setting

ORIGINAL = b"""# Explicit model stays independent
model = "gpt-6.2-terra"
cli_auth_credentials_store = "file"

[agents]
# Settings owned by the operator
default_subagent_model = "gpt-6.2-terra" # model comment
default_subagent_reasoning_effort = "medium"

[some_other_section]
keep = "DO_NOT_PRINT_CONFIG"
"""

OLD = "gpt-6.2-terra"
NEXT = "gpt-6.3-terra"


def native_state(config: Path):
    raw = tomllib.loads(config.read_text())
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    return raw, f"sha256:{digest}"


class FakeAppServer:
    def __init__(self, config: Path) -> None:
        self.config = config
        self.writes = []
        self.successor = NEXT
        self.disposition = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def request(self, method, params):
        document, version = native_state(self.config)
        name = {"type": "user", "file": str(self.config), "profile": None}
        if method == "config/read":
            assert params == {"includeLayers": True}
            origins = {
                f"agents.{key}": {"name": name, "version": version}
                for key in (setting.MODEL_KEY, setting.EFFORT_KEY)
            }
            if self.disposition == "managed":
                origins[f"agents.{setting.MODEL_KEY}"] = {
                    "name": {"type": "mdm"},
                    "version": version,
                }
            return {
                "config": document,
                "layers": [{"name": name, "version": version, "config": document}],
                "origins": origins,
            }
        assert method == "config/value/write"
        self.writes.append(params)
        assert params == {
            "filePath": str(self.config),
            "expectedVersion": version,
            "keyPath": "agents.default_subagent_model",
            "mergeStrategy": "replace",
            "value": self.successor,
        }
        if self.disposition == "conflict":
            raise setting.SettingError("owner_changed")
        model = document["agents"][setting.MODEL_KEY]
        updated = self.config.read_bytes().replace(
            f'default_subagent_model = "{model}"'.encode(),
            f'default_subagent_model = "{self.successor}"'.encode(),
        )
        self.config.write_bytes(updated)
        if self.disposition == "lost":
            raise setting.SettingError("app_server_timeout")
        if self.disposition == "extra-effect":
            self.config.write_bytes(updated.replace(b"DO_NOT_PRINT_CONFIG", b"unexpected-effect"))
        _, version = native_state(self.config)
        return {"version": version, "status": "ok", "filePath": str(self.config)}


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "config.toml"
    path.write_bytes(ORIGINAL)
    server = FakeAppServer(path)
    monkeypatch.setattr(setting, "_AppServer", lambda config, executable: server)
    return path, server


def update(path, snapshot, successor=NEXT):
    return setting.update_setting(
        path,
        expected_model=snapshot.model,
        expected_effort=snapshot.effort,
        expected_version=snapshot.version,
        successor=successor,
    )


def test_writes_only_the_model_and_reports_exact_readback(config):
    path, server = config
    original = setting.read_setting(path)
    assert (original.model, original.effort) == (OLD, "medium")
    result = update(path, original)
    assert result.status == "updated"
    assert result.setting == setting.read_setting(path)
    assert result.setting.model == NEXT
    assert result.setting.effort == original.effort
    assert result.setting.version != original.version
    assert len(server.writes) == 1
    assert path.read_bytes() == ORIGINAL.replace(
        f'default_subagent_model = "{OLD}"'.encode(),
        f'default_subagent_model = "{NEXT}"'.encode(),
    )


def test_current_version_and_same_model_is_a_no_op(config):
    path, server = config
    snapshot = setting.read_setting(path)
    assert update(path, snapshot, snapshot.model) == setting.UpdateResult("unchanged", snapshot)
    assert not server.writes
    assert path.read_bytes() == ORIGINAL


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b"# Settings owned by the operator", b"# A changed owner comment"),
        (b'reasoning_effort = "medium"', b'reasoning_effort = "high"'),
        (b"DO_NOT_PRINT_CONFIG", b"one unrelated changed value"),
        (
            f'default_subagent_model = "{OLD}"'.encode(),
            b'default_subagent_model = "gpt-6.4-terra"',
        ),
    ],
)
def test_detects_any_intervening_owner_edit_before_writing(config, before, after):
    path, server = config
    snapshot = setting.read_setting(path)
    changed = ORIGINAL.replace(before, after)
    path.write_bytes(changed)
    with pytest.raises(setting.SettingError, match="owner_changed"):
        update(path, snapshot)
    assert not server.writes
    assert path.read_bytes() == changed


def test_version_bound_to_one_file_but_native_values_can_match(config, monkeypatch):
    path, _server = config
    snapshot = setting.read_setting(path)
    another = path.parent / "copy" / "config.toml"
    another.parent.mkdir()
    another.write_bytes(ORIGINAL)
    other_server = FakeAppServer(another)
    monkeypatch.setattr(setting, "_AppServer", lambda config, executable: other_server)
    other = setting.read_setting(another)
    assert other.model == snapshot.model
    assert other.version.split("/")[0] == snapshot.version.split("/")[0]
    with pytest.raises(setting.SettingError, match="owner_changed"):
        update(another, snapshot)
    assert not other_server.writes


def test_managed_or_overridden_effective_key_fails_closed(config):
    path, server = config
    snapshot = setting.read_setting(path)
    server.disposition = "managed"
    with pytest.raises(setting.SettingError, match="setting_overridden"):
        update(path, snapshot)
    assert not server.writes


@pytest.mark.parametrize(
    ("disposition", "reason"),
    [
        ("conflict", "owner_changed"),
        ("lost", "write_unconfirmed"),
        ("extra-effect", "write_unconfirmed"),
    ],
)
def test_never_retries_rejected_or_unconfirmed_native_write(config, disposition, reason):
    path, server = config
    snapshot = setting.read_setting(path)
    server.disposition = disposition
    with pytest.raises(setting.SettingError, match=reason):
        update(path, snapshot)
    assert len(server.writes) == 1
    if disposition == "conflict":
        assert path.read_bytes() == ORIGINAL


def test_cli_prints_selected_receipt_only(config, capsys):
    path, _server = config
    assert setting.main(["--config", str(path), "read"]) == 0
    out = capsys.readouterr().out
    assert "DO_NOT_PRINT_CONFIG" not in out
    assert set(json.loads(out)) == {"status", "model", "effort", "version"}


def test_native_stdio_timeout_leaves_file_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_bytes(ORIGINAL)
    child = tmp_path / "silent-app-server"
    child.write_text("#!/bin/sh\nexec /bin/sleep 5\n")
    child.chmod(0o700)
    monkeypatch.setattr(setting, "REQUEST_TIMEOUT", 0.05)
    with pytest.raises(setting.SettingError, match="app_server_timeout"):
        setting.read_setting(path, executable=child)
    assert path.read_bytes() == ORIGINAL


@pytest.mark.parametrize(("model", "effort"), [("gpt-6.4-terra", "medium"), (OLD, "high")])
def test_expected_owner_values_are_checked_even_at_current_version(config, model, effort):
    path, server = config
    snapshot = setting.read_setting(path)
    with pytest.raises(setting.SettingError, match="owner_changed"):
        setting.update_setting(
            path,
            expected_model=model,
            expected_effort=effort,
            expected_version=snapshot.version,
            successor=NEXT,
        )
    assert not server.writes


def test_unrecognized_config_values_cannot_be_printed(config, capsys):
    path, _server = config
    path.write_bytes(ORIGINAL.replace(b'"medium"', b'"sk-this-must-not-be-printed"'))
    assert setting.main(["--config", str(path), "read"]) == 2
    reply = capsys.readouterr().out
    assert "sk-this-must-not-be-printed" not in reply
    assert json.loads(reply) == {"status": "blocked", "reason": "unsupported_setting"}


def test_foreign_family_is_refused_without_a_write(config):
    path, server = config
    before = setting.read_setting(path)
    with pytest.raises(setting.SettingError, match="different_model_family"):
        update(path, before, "gpt-6.3-luna")
    assert not server.writes
    assert path.read_bytes() == ORIGINAL


def test_same_family_rollback_remains_valid(config):
    path, server = config
    upgraded = update(path, setting.read_setting(path))
    server.successor = OLD
    rollback = update(path, upgraded.setting, OLD)
    assert rollback.status == "updated" and rollback.setting.model == OLD
    assert len(server.writes) == 2
    assert path.read_bytes() == ORIGINAL
