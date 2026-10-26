"""Synthetic-only borrower tests: never open a real account credential or env value."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

import closer_claude_auth as auth

TOKEN = "synthetic-oauth_ONLY_not-a-credential-123"


@pytest.fixture
def synthetic_home(tmp_path, monkeypatch):
    home = (tmp_path / "synthetic-account-home").resolve()
    home.mkdir()
    directory = home / ".codex" / "handoff"
    directory.mkdir(parents=True)
    path = directory / ".claude-oauth-token"
    path.write_text("export CLAUDE_CODE_OAUTH_TOKEN=" + TOKEN + "\n")
    path.chmod(0o600)
    # TEST-ONLY replacement of the UID account anchor. Public production API
    # has no arbitrary file path, uid, HOME setting or credential env override.
    monkeypatch.setattr(auth, "_ACCOUNT_HOME", home)
    return home, path


@pytest.mark.parametrize("prefix", ["", "export "])
@pytest.mark.parametrize("quote", ["", "'", '"'])
@pytest.mark.parametrize("newline", ["", "\n"])
def test_single_literal_assignment_yields_then_releases_reference(
    synthetic_home, prefix, quote, newline
):
    home, path = synthetic_home
    path.write_text(prefix + "CLAUDE_CODE_OAUTH_TOKEN=" + quote + TOKEN + quote + newline)
    before = path.read_bytes(), path.stat().st_mode
    context = auth.borrow_claude_oauth_token(real_home=home)
    assert TOKEN not in repr(context)
    with context as borrowed:
        assert borrowed == TOKEN
    assert context.gen.gi_frame is None
    assert (path.read_bytes(), path.stat().st_mode) == before


def test_caller_failure_also_closes_generator_and_drops_internal_token(synthetic_home):
    home, _ = synthetic_home
    context = auth.borrow_claude_oauth_token(real_home=home)
    with pytest.raises(RuntimeError, match="synthetic caller"):
        with context as borrowed:
            assert borrowed == TOKEN
            raise RuntimeError("synthetic caller")
    assert context.gen.gi_frame is None


@pytest.mark.parametrize(
    "body",
    [
        "",
        "CLAUDE_CODE_OAUTH_TOKEN=",
        "export CLAUDE_CODE_OAUTH_TOKEN=''\n",
        "CLAUDE_CODE_OAUTH_TOKEN=" + TOKEN + "\nCLAUDE_CODE_OAUTH_TOKEN=second\n",
        "export CLAUDE_CODE_OAUTH_TOKEN=" + TOKEN + "\nexport OTHER=value\n",
        "# comment\nCLAUDE_CODE_OAUTH_TOKEN=" + TOKEN,
        "CLAUDE_CODE_OAUTH_TOKEN=$OTHER",
        "CLAUDE_CODE_OAUTH_TOKEN=${OTHER}",
        "CLAUDE_CODE_OAUTH_TOKEN=$(echo stolen)",
        "CLAUDE_CODE_OAUTH_TOKEN=`echo stolen`",
        "CLAUDE_CODE_OAUTH_TOKEN=token;echo stolen",
        "CLAUDE_CODE_OAUTH_TOKEN=token && true",
        "CLAUDE_CODE_OAUTH_TOKEN=~/.private",
        "CLAUDE_CODE_OAUTH_TOKEN=token\\nmore",
        'CLAUDE_CODE_OAUTH_TOKEN="token$OTHER"',
        "CLAUDE_CODE_OAUTH_TOKEN='token\nmore'",
        "CLAUDE_CODE_OAUTH_TOKEN=token\x00more",
        "CLAUDE_CODE_OAUTH_TOKEN=token\r\n",
        " CLAUDE_CODE_OAUTH_TOKEN=token",
        "export\tCLAUDE_CODE_OAUTH_TOKEN=token",
        "CLAUDE_CODE_OAUTH_TOKEN =token",
        "CLAUDE_CODE_OAUTH_TOKEN=token \n",
        "OTHER=token",
        "CLAUDE_CODE_OAUTH_TOKEN=秘密",
        "CLAUDE_CODE_OAUTH_TOKEN=token\n\n",
    ],
)
def test_unknown_or_shell_formats_refuse_without_secret_diagnostics(
    synthetic_home, body, capsys, caplog
):
    home, path = synthetic_home
    path.write_text(body)
    with pytest.raises(auth.ClaudeAuthUnavailable) as failure:
        with auth.borrow_claude_oauth_token(real_home=home):
            pytest.fail("invalid assignment yielded a value")
    assert TOKEN not in str(failure.value)
    assert not capsys.readouterr().out and not caplog.records
    # Failed parser frames must not retain a decoded secret or regex match.
    trace = failure.value.__traceback__
    while trace:
        if trace.tb_frame.f_code.co_name == "_literal_assignment":
            assert trace.tb_frame.f_locals["document"] is None
            assert trace.tb_frame.f_locals["match"] is None
        trace = trace.tb_next


def test_wrong_real_home_refuses_before_any_file_open(synthetic_home, monkeypatch, tmp_path):
    _, _path = synthetic_home
    monkeypatch.setattr(auth.os, "open", lambda *_a, **_kw: pytest.fail("wrong home opened file"))
    with pytest.raises(auth.ClaudeAuthUnavailable, match="real_home_differs"):
        with auth.borrow_claude_oauth_token(real_home=tmp_path / "foreign"):
            pytest.fail("foreign home accepted")


@pytest.mark.parametrize("mode", [0o000, 0o400, 0o640, 0o644, 0o660, 0o700, 0o1600])
def test_exact_regular_file_0600_required(synthetic_home, mode):
    home, path = synthetic_home
    path.chmod(mode)
    with pytest.raises(auth.ClaudeAuthUnavailable):
        with auth.borrow_claude_oauth_token(real_home=home):
            pytest.fail("wrong mode yielded")
    assert stat.S_IMODE(path.lstat().st_mode) == mode


@pytest.mark.parametrize("component", ["file", "handoff", ".codex"])
def test_symlink_at_any_credential_component_refuses(synthetic_home, tmp_path, component):
    home, path = synthetic_home
    selected = {"file": path, "handoff": path.parent, ".codex": path.parent.parent}[component]
    target = tmp_path / ("moved-" + component.replace(".", ""))
    selected.rename(target)
    selected.symlink_to(target, target_is_directory=component != "file")
    with pytest.raises(auth.ClaudeAuthUnavailable):
        with auth.borrow_claude_oauth_token(real_home=home):
            pytest.fail("symlink yielded")
    assert selected.is_symlink()


def test_directory_in_place_of_file_refuses(synthetic_home):
    home, path = synthetic_home
    path.unlink()
    path.mkdir(mode=0o600)
    with pytest.raises(auth.ClaudeAuthUnavailable):
        with auth.borrow_claude_oauth_token(real_home=home):
            pytest.fail("directory yielded")


def test_wrong_uid_refuses_before_read(synthetic_home, monkeypatch):
    home, _ = synthetic_home
    real_stat = auth.os.stat

    def observed(path, **kwargs):
        value = real_stat(path, **kwargs)
        if str(path) == ".claude-oauth-token":
            return SimpleNamespace(
                st_mode=value.st_mode, st_uid=os.getuid() + 1, st_size=value.st_size
            )
        return value

    monkeypatch.setattr(auth.os, "stat", observed)
    monkeypatch.setattr(auth.os, "read", lambda *_: pytest.fail("foreign uid read"))
    with pytest.raises(auth.ClaudeAuthUnavailable):
        with auth.borrow_claude_oauth_token(real_home=home):
            pytest.fail("foreign uid yielded")


def test_oversize_refuses_before_read(synthetic_home, monkeypatch):
    home, path = synthetic_home
    path.write_bytes(b"x" * (auth.MAX_CREDENTIAL_BYTES + 1))
    monkeypatch.setattr(auth.os, "read", lambda *_: pytest.fail("oversize read"))
    with pytest.raises(auth.ClaudeAuthUnavailable):
        with auth.borrow_claude_oauth_token(real_home=home):
            pytest.fail("oversize yielded")


def test_replacement_between_before_stat_and_open_refuses(synthetic_home, monkeypatch):
    home, path = synthetic_home
    real_open = auth.os.open

    def raced(name, flags, *args, **kwargs):
        if str(name) == ".claude-oauth-token":
            original = path.with_name("original-synthetic")
            path.rename(original)
            path.write_text("CLAUDE_CODE_OAUTH_TOKEN=replacement\n")
            path.chmod(0o600)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(auth.os, "open", raced)
    with pytest.raises(auth.ClaudeAuthUnavailable, match="file_changed"):
        with auth.borrow_claude_oauth_token(real_home=home):
            pytest.fail("replacement yielded")


@pytest.mark.parametrize("race", ["replace", "chmod", "append"])
def test_change_during_actual_read_refuses_and_closes_all_fds(synthetic_home, monkeypatch, race):
    home, path = synthetic_home
    real_read, real_close = auth.os.read, auth.os.close
    reads, closed = [], []

    def raced(fd, amount):
        result = real_read(fd, amount)
        if result and not reads:
            reads.append(True)
            if race == "replace":
                replacement = path.with_name("replacement-synthetic")
                replacement.write_text("CLAUDE_CODE_OAUTH_TOKEN=replacement\n")
                replacement.chmod(0o600)
                replacement.replace(path)
            elif race == "chmod":
                path.chmod(0o644)
            else:
                with path.open("a") as stream:
                    stream.write("\n")
        return result

    def close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(auth.os, "read", raced)
    monkeypatch.setattr(auth.os, "close", close)
    with pytest.raises(auth.ClaudeAuthUnavailable):
        with auth.borrow_claude_oauth_token(real_home=home):
            pytest.fail("raced bytes yielded")
    assert reads and len(closed) == 4 and len(set(closed)) == 4


def test_no_environment_fallback_or_subprocess_hash_log(
    synthetic_home, monkeypatch, caplog, capsys
):
    import hashlib
    import subprocess

    home, path = synthetic_home

    class NoCredentialLookup(dict):
        def __getitem__(self, key):
            if key == "CLAUDE_CODE_OAUTH_TOKEN":
                pytest.fail("credential environment lookup")
            return super().__getitem__(key)

        def get(self, key, default=None):
            if key == "CLAUDE_CODE_OAUTH_TOKEN":
                pytest.fail("credential environment lookup")
            return super().get(key, default)

        def __contains__(self, key):
            if key == "CLAUDE_CODE_OAUTH_TOKEN":
                pytest.fail("credential environment lookup")
            return super().__contains__(key)

    # Replace the mapping object, without copying/reading any live env value.
    # In particular setenv must not save an existing credential for restoration.
    monkeypatch.setattr(auth.os, "environ", NoCredentialLookup({"HOME": "/foreign-home"}))
    monkeypatch.setattr(hashlib, "sha256", lambda *_a, **_kw: pytest.fail("credential hashed"))
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("auth/provider command"))
    with auth.borrow_claude_oauth_token(real_home=home) as borrowed:
        assert borrowed == TOKEN
    path.unlink()
    with pytest.raises(auth.ClaudeAuthUnavailable):
        with auth.borrow_claude_oauth_token(real_home=home):
            pytest.fail("ambient fallback used")
    assert not path.exists() and not caplog.records
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("mode", [0o1600, 0o2600, 0o4600])
def test_special_mode_bits_refused_even_if_host_filesystem_strips_them(mode):
    value = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=os.getuid(), st_size=64)
    with pytest.raises(auth.ClaudeAuthUnavailable):
        auth._check_file(value, os.getuid())


@pytest.mark.parametrize("component", ["home", ".codex", "handoff"])
@pytest.mark.parametrize("mode", [0o770, 0o777])
def test_group_or_world_writable_ancestor_refuses_before_secret_read(
    synthetic_home, monkeypatch, component, mode
):
    home, path = synthetic_home
    selected = {"home": home, ".codex": path.parent.parent, "handoff": path.parent}[component]
    selected.chmod(mode)
    before = selected.stat().st_mode
    monkeypatch.setattr(auth.os, "read", lambda *_: pytest.fail("unsafe ancestry read credential"))
    with pytest.raises(
        auth.ClaudeAuthUnavailable, match="directory_identity_or_permissions_invalid"
    ):
        with auth.borrow_claude_oauth_token(real_home=home):
            pytest.fail("writable ancestor yielded")
    assert selected.stat().st_mode == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("component", ["home", ".codex", "handoff"])
@pytest.mark.parametrize("mode", [0o700, 0o755])
def test_nonwritable_group_and_world_ancestry_remains_supported(synthetic_home, component, mode):
    home, path = synthetic_home
    selected = {"home": home, ".codex": path.parent.parent, "handoff": path.parent}[component]
    selected.chmod(mode)
    before = selected.stat().st_mode
    with auth.borrow_claude_oauth_token(real_home=home) as borrowed:
        assert borrowed == TOKEN
    assert selected.stat().st_mode == before
