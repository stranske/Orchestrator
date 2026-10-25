"""Metadata fixtures and injected runners only: never access the real keychain."""

import datetime as dt
import hashlib
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/usage_poll/prune-claude-keychain-slots.py"
NOW = dt.datetime(2026, 10, 22, 12, tzinfo=dt.UTC)


@pytest.fixture
def prune():
    spec = importlib.util.spec_from_file_location("prune_claude_keychain_slots", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def item(service, modified):
    # Real dump ordering: mdat precedes svce, with a new keychain header per item.
    metadata = f'    "mdat"<timedate>=0x32303236 "{modified}Z"\n' if modified else ""
    return (
        'keychain: "/fixture/login.keychain-db"\n'
        'class: "genp"\nattributes:\n' + metadata + f'    "svce"<blob>="{service}"\n'
    )


def test_parse_metadata_stays_with_its_item(prune):
    dump = (
        item("unrelated", "20200101000000")
        + item("Claude Code-credentials-11111111", "20260901000000")
        + item("Claude Code-credentials-22222222", "20261022120000")
        + item("Claude Code-credentials-33333333", None)
        + item("Claude Code-credentials-44444444", "20261399000000")
        + item("Claude Code-credentials", "20200101000000")
    )
    rows = prune.parse_slots(dump)
    assert rows == [
        ("Claude Code-credentials-11111111", "11111111", dt.datetime(2026, 9, 1, tzinfo=dt.UTC)),
        ("Claude Code-credentials-22222222", "22222222", NOW),
        ("Claude Code-credentials-33333333", "33333333", None),
        ("Claude Code-credentials-44444444", "44444444", None),
        ("Claude Code-credentials", None, dt.datetime(2020, 1, 1, tzinfo=dt.UTC)),
    ]


@pytest.mark.parametrize(
    "age,delete",
    [
        (dt.timedelta(days=30, seconds=1), True),
        (dt.timedelta(days=30), False),
        (dt.timedelta(days=29, hours=23), False),
        (dt.timedelta(days=-1), False),
    ],
)
def test_selection_strictly_older_than_cutoff(prune, age, delete):
    row = ("Claude Code-credentials-11111111", "11111111", NOW - age)
    doomed, kept = prune.select_slots([row], now=NOW, poller="aaaaaaaa")
    assert bool(doomed) is delete
    assert bool(kept) is not delete


def test_selection_always_protects_default_poller_extra_and_unknown(prune):
    old = NOW - dt.timedelta(days=300)
    rows = [
        ("Claude Code-credentials", None, old),
        ("Claude Code-credentials-aaaaaaaa", "aaaaaaaa", old),
        ("Claude Code-credentials-bbbbbbbb", "bbbbbbbb", old),
        ("Claude Code-credentials-cccccccc", "cccccccc", None),
        ("Claude Code-credentials-dddddddd", "dddddddd", old),
    ]
    doomed, kept = prune.select_slots(rows, now=NOW, poller="aaaaaaaa", keep=["bbbbbbbb"])
    assert [name for name, _ in doomed] == [rows[-1][0]]
    assert [name for name, _ in kept] == [row[0] for row in rows[:-1]]


def test_larger_threshold_and_minimum_age(prune):
    row = ("Claude Code-credentials-11111111", "11111111", NOW - dt.timedelta(days=45))
    assert not prune.select_slots([row], now=NOW, poller="aaaaaaaa", days=60)[0]
    with pytest.raises(ValueError, match="at least 30"):
        prune.select_slots([row], now=NOW, poller="aaaaaaaa", days=29)


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("delete_rc", [0, 1])
def test_cli_uses_only_injected_runner(prune, tmp_path, capsys, monkeypatch, apply, delete_rc):
    monkeypatch.delenv("CLAUDE_POLL_CONFIG_DIR", raising=False)
    poller = hashlib.sha256(str(tmp_path / ".codex/claude-poll").encode()).hexdigest()[:8]
    dump = (
        item("Claude Code-credentials", "20200101000000")
        + item(f"Claude Code-credentials-{poller}", "20200101000000")
        + item("Claude Code-credentials-11111111", "20260922115959")
        + item("Claude Code-credentials-22222222", "20260922120000")
        + item("Claude Code-credentials-33333333", None)
    )
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "dump-keychain":
            return SimpleNamespace(stdout=dump, returncode=0)
        assert command == [
            "security",
            "delete-generic-password",
            "-s",
            "Claude Code-credentials-11111111",
            str(tmp_path / "Library/Keychains/login.keychain-db"),
        ]
        return SimpleNamespace(returncode=delete_rc)

    rc = prune.main(["--apply"] if apply else [], runner=runner, home=tmp_path, now=NOW)
    assert rc == (delete_rc if apply else 0)
    assert calls[0] == (
        ["security", "dump-keychain", str(tmp_path / "Library/Keychains/login.keychain-db")],
        {"capture_output": True, "text": True, "check": True},
    )
    assert len(calls) == (2 if apply else 1)
    output = capsys.readouterr().out
    assert "keep: 4 | prune: 1" in output
    assert ("DRY RUN: nothing deleted" in output) is not apply


@pytest.mark.parametrize("override", ["", "/fixture/custom poller"])
def test_cli_protects_configured_and_default_poller(prune, tmp_path, monkeypatch, override):
    monkeypatch.setenv("CLAUDE_POLL_CONFIG_DIR", override)
    default_dir = str(tmp_path / ".codex/claude-poll")
    directories = {default_dir, override or default_dir}
    dump = item("Claude Code-credentials", "20200101000000")
    for directory in directories:
        suffix = hashlib.sha256(directory.encode()).hexdigest()[:8]
        dump += item(f"Claude Code-credentials-{suffix}", "20200101000000")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        assert command[1] == "dump-keychain", "protected slot must never be deleted"
        return SimpleNamespace(stdout=dump, returncode=0)

    assert prune.main(["--apply"], runner=runner, home=tmp_path, now=NOW) == 0
    assert len(calls) == 1


def test_cli_refuses_lower_threshold_before_reading(prune, capsys):
    def runner(*args, **kwargs):
        pytest.fail("invalid threshold must not access keychain")

    with pytest.raises(SystemExit) as error:
        prune.main(["--apply", "--days", "29"], runner=runner)
    assert error.value.code == 2
    assert "--days must be at least 30" in capsys.readouterr().err


def test_failed_metadata_read_never_deletes_or_echoes_dump(prune, capsys):
    def runner(command, **kwargs):
        assert command[1] == "dump-keychain"
        raise subprocess.CalledProcessError(1, command, output="UNTRUSTED_OUTPUT")

    assert prune.main(["--apply"], runner=runner) == 1
    output = capsys.readouterr()
    assert "nothing deleted" in output.err
    assert "UNTRUSTED_OUTPUT" not in output.out + output.err


def test_cli_apply_keeps_explicit_suffix_and_deletes_other_old_slots(prune, tmp_path, capsys):
    suffixes = ["11111111", "22222222", "33333333"]
    dump = "".join(item(f"Claude Code-credentials-{s}", "20200101000000") for s in suffixes)
    calls = []
    keychain = str(tmp_path / "Library/Keychains/login.keychain-db")

    def runner(command, **kwargs):
        calls.append(command)
        if command == ["security", "dump-keychain", keychain]:
            return SimpleNamespace(stdout=dump, returncode=0)
        assert command in [
            ["security", "delete-generic-password", "-s", f"Claude Code-credentials-{s}", keychain]
            for s in ("11111111", "33333333")
        ], "--keep must survive CLI parsing and selection"
        return SimpleNamespace(returncode=0)

    assert prune.main(["--apply", "--keep", "22222222"], runner=runner, home=tmp_path, now=NOW) == 0
    assert calls == [["security", "dump-keychain", keychain]] + [
        ["security", "delete-generic-password", "-s", f"Claude Code-credentials-{s}", keychain]
        for s in ("11111111", "33333333")
    ]
    output = capsys.readouterr().out
    assert "KEEP  Claude Code-credentials-22222222" in output
    assert "deleted 2, failed 0" in output
