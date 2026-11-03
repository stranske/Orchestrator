"""Fresh, inert private Git-environment data for the reviewed CSV consumer.

This module creates only a closed local configuration tree.  Publication still
owns all native credential, assignee, author, profile, budget, and mutation
checks; this data is never an execution permit.
"""

from __future__ import annotations

import copy
import hashlib
import os
import shlex
import stat
from pathlib import Path
from typing import Any, cast

REVIEWED_SOURCE_SHA = "8a4716f844fe002c6606b6bad4104c74be83d32c"
REVIEWED_REPOSITORY = "stranske/orchestrator"
REVIEWED_REPOSITORY_ID = 1342306972
REVIEWED_CONSUMER_ID = "test-csv-followup988-codex"
REVIEWED_WRITER_ID = 23046322
REVIEWED_WRITER_LOGIN = "stranske"
REVIEWED_WRITER_TYPE = "User"
ORIGINAL_GIT_HOME = "/private/tmp/closer-publication989-local-native-preparation-20261028/git-home"
ORIGINAL_CONFIG_SHA256 = "0f8170f4a447232d445ad5fff35d973200774fa42ff01a27da6394ae7e3bbdd8"
REVIEWED_TRUSTED_CONTEXT_SHA256 = "73df9369a51ec4d515c1dfcabec4bb0ee657ce9f0d93161b53c458f34a543745"
REVIEWED_PROFILE_ID = "codex-6.1-sol-high"
REVIEWED_PROFILE_SHA256 = "0bdf1d6cf20e0c9a082c1680b07934422d46676dcff242266937bc240d2fed62"
REVIEWED_ACCOUNT_SHA256 = "b35623789b2cd8b3f5977b456bcfb0b15e5fc6bc2a38c32a590acc0f69f5f7ef"
REVIEWED_GH_PATH = "/opt/homebrew/Cellar/gh/2.94.0/bin/gh"
REVIEWED_GH_SHA256 = "2ef6c63bc52dc32bc42366215f91a51b009950791806209fd7f6fc7f5b668ba2"
REVIEWED_ALLOWED_PATHS = (
    "tests/fixtures/closer_real_csv_canary/v1/csv_export.py",
    "tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py",
)
_PRIVATE_PARENT = "controller-private"
_PRIVATE_LEAF = "publication-environment"


class GitEnvironmentHeld(RuntimeError):
    pass


def _need(value: object, reason: str) -> None:
    if not value:
        raise GitEnvironmentHeld(reason)


def _absolute_path(path: str | Path) -> Path:
    value = Path(path)
    _need(value.is_absolute() and ".." not in value.parts, "private Git root is not canonical")
    return value


def _dedicated_private_path(path: str | Path) -> Path:
    """Return only the canonical dedicated-child shape, without filesystem state."""
    value = _absolute_path(path)
    _need(
        value.name == _PRIVATE_LEAF and value.parent.name == _PRIVATE_PARENT,
        "private Git root is not the dedicated publication environment",
    )
    _need(not value.is_symlink(), "private Git root is redirected")
    return value


def _fresh_dedicated_private_root(path: str | Path) -> Path:
    """Require the provisioner's fresh child and protected existing ancestors."""
    value = _dedicated_private_path(path)
    _need(not value.exists() and not value.is_symlink(), "private Git root already exists")
    parent = value.parent
    try:
        metadata = parent.stat(follow_symlinks=False)
        _need(
            stat.S_ISDIR(metadata.st_mode)
            and not parent.is_symlink()
            and parent.resolve(strict=True) == parent
            and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700,
            "private Git parent differs",
        )
        # A root-owned sticky temporary directory is the sole writable
        # ancestor accepted; it is the conventional protected Linux CI root.
        for ancestor in parent.parents:
            item = ancestor.stat(follow_symlinks=False)
            mode = stat.S_IMODE(item.st_mode)
            protected = not mode & 0o022
            root_sticky_temp = item.st_uid == 0 and bool(mode & stat.S_ISVTX)
            _need(
                stat.S_ISDIR(item.st_mode)
                and not ancestor.is_symlink()
                and item.st_uid in {0, os.getuid()}
                and (protected or root_sticky_temp),
                "private Git ancestor differs",
            )
    except OSError as exc:
        raise GitEnvironmentHeld("private Git parent unavailable") from exc
    return value


def _config_bytes(gh_path: str | Path) -> bytes:
    """Source-selected public Git identity and the fixed existing GH helper."""
    path = _absolute_path(gh_path)
    helper = "!" + shlex.quote(str(path)) + " auth git-credential"
    return (
        "[user]\n"
        "\tname = stranske\n"
        "\temail = 23046322+stranske@users.noreply.github.com\n"
        "[credential]\n"
        "\thelper =\n"
        f"\thelper = {helper}\n"
    ).encode("utf-8")


def _consumer(policy: dict[str, Any], consumer_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        config = policy["correction_controller_installation"]["config"]
        consumers = config["consumers"]
        consumer = consumers[consumer_id]
    except (KeyError, TypeError) as exc:
        raise GitEnvironmentHeld("reviewed coding consumer unavailable") from exc
    _need(
        isinstance(config, dict)
        and isinstance(consumers, dict)
        and set(consumers) == {consumer_id}
        and isinstance(consumer, dict),
        "reviewed coding consumer differs",
    )
    return config, consumer


def _publication_policy(consumer: dict[str, Any], consumer_id: str) -> dict[str, Any]:
    try:
        import closer_coding_publication as publication

        result = publication.validate_consumer_mode(consumer_id, consumer)
    except (ImportError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise GitEnvironmentHeld("publication policy invalid") from exc
    _need(isinstance(result, dict), "reviewed publication policy unavailable")
    return result


def _reviewed_consumer(
    policy: dict[str, Any], consumer_id: str, *, old_source: bool
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    config, consumer = _consumer(policy, consumer_id)
    _need(
        config.get("repository") == REVIEWED_REPOSITORY
        and config.get("repository_id") == REVIEWED_REPOSITORY_ID
        and config.get("writer_id") == REVIEWED_WRITER_ID
        and config.get("writer_login") == REVIEWED_WRITER_LOGIN
        and config.get("writer_type") == REVIEWED_WRITER_TYPE
        and (not old_source or config.get("source_sha") == REVIEWED_SOURCE_SHA),
        "reviewed coding controller identity differs",
    )
    try:
        import closer_coding_session_v2 as coding_session

        coding = coding_session.validate_policy(consumer[coding_session.POLICY_FIELD])
    except (ImportError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise GitEnvironmentHeld("reviewed coding policy invalid") from exc
    _need(
        consumer.get("runner") == "existing-local-controller"
        and consumer.get("agent") == "codex"
        and consumer.get("owner_login") == REVIEWED_WRITER_LOGIN
        and consumer.get("account_sha256") == REVIEWED_ACCOUNT_SHA256
        and consumer.get("profile_id") == REVIEWED_PROFILE_ID
        and consumer.get("profile_sha256") == REVIEWED_PROFILE_SHA256
        and coding.get("consumer_id") == consumer_id
        and coding.get("agent") == "codex"
        and coding.get("profile_id") == REVIEWED_PROFILE_ID
        and coding.get("profile_sha256") == REVIEWED_PROFILE_SHA256,
        "reviewed coding consumer differs",
    )
    publication = _publication_policy(consumer, consumer_id)
    _need(
        publication.get("allowed_path_prefixes") == list(REVIEWED_ALLOWED_PATHS)
        and publication.get("trusted_context_sha256") == REVIEWED_TRUSTED_CONTEXT_SHA256,
        "reviewed coding publication scope differs",
    )
    bootstrap = consumer.get("native_bootstrap")
    _need(
        isinstance(bootstrap, dict)
        and bootstrap.get("gh_path") == REVIEWED_GH_PATH
        and bootstrap.get("gh_sha256") == REVIEWED_GH_SHA256,
        "pinned GH helper differs",
    )
    return config, consumer, publication


def rebase_private_git_environment(
    policy: dict[str, Any],
    *,
    old_source_sha: str,
    consumer_id: str,
    private_root: str | Path,
    gh_path: str | Path,
) -> dict[str, Any]:
    """Project only the reviewed old Git-home pin onto a fresh private root."""
    _need(
        old_source_sha == REVIEWED_SOURCE_SHA and consumer_id == REVIEWED_CONSUMER_ID,
        "unreviewed Git-environment predecessor",
    )
    projected = copy.deepcopy(policy)
    _need(str(_absolute_path(gh_path)) == REVIEWED_GH_PATH, "unreviewed Git helper route")
    _config, consumer, publication_policy = _reviewed_consumer(
        projected, consumer_id, old_source=True
    )
    old_environment = publication_policy.get("git_environment_v1")
    _need(
        isinstance(old_environment, dict)
        and old_environment
        == {
            "schema": "closer-publication-private-git-environment/v1",
            "home": ORIGINAL_GIT_HOME,
            "config_sha256": ORIGINAL_CONFIG_SHA256,
        },
        "reviewed private Git environment differs",
    )
    root = _dedicated_private_path(private_root)
    fresh_environment = {
        "schema": "closer-publication-private-git-environment/v1",
        "home": str(root / "git-home"),
        "config_sha256": hashlib.sha256(_config_bytes(gh_path)).hexdigest(),
    }
    consumer["publication_v2"] = {
        **consumer["publication_v2"],
        "git_environment_v1": fresh_environment,
    }
    _need(
        _publication_policy(consumer, consumer_id) == consumer["publication_v2"],
        "rebased publication policy invalid",
    )
    changed = []
    before = policy["correction_controller_installation"]["config"]["consumers"][consumer_id]
    after = consumer
    for key in set(before) | set(after):
        if key == "publication_v2":
            continue
        if before.get(key) != after.get(key):
            changed.append(key)
    _need(not changed, "Git-environment projection changed consumer fields")
    before_publication = before["publication_v2"]
    for key in set(before_publication) | set(after["publication_v2"]):
        if key != "git_environment_v1":
            _need(
                before_publication.get(key) == after["publication_v2"].get(key),
                "Git-environment projection changed publication fields",
            )
    return projected


def _mkdir_new(path: Path, created: list[Path]) -> None:
    os.mkdir(path, 0o700)
    created.append(path)
    metadata = path.stat(follow_symlinks=False)
    _need(
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and not path.is_symlink(),
        "fresh private Git directory differs",
    )


def _write_new(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            _need(count > 0, "private Git config write incomplete")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def provision_private_git_environment(
    policy: dict[str, Any], *, consumer_id: str, private_root: str | Path
) -> dict[str, Any]:
    """Create one fresh inert private Git home and return data-only facts."""
    _need(consumer_id == REVIEWED_CONSUMER_ID, "unreviewed Git-environment consumer")
    _config, consumer, publication_policy = _reviewed_consumer(
        policy, consumer_id, old_source=False
    )
    bootstrap = consumer.get("native_bootstrap")
    _need(isinstance(bootstrap, dict), "pinned GH helper unavailable")
    bootstrap = cast(dict[str, Any], bootstrap)
    gh_path = _absolute_path(bootstrap.get("gh_path", ""))
    expected_raw = _config_bytes(gh_path)
    root = _fresh_dedicated_private_root(private_root)
    environment = publication_policy.get("git_environment_v1")
    _need(
        isinstance(environment, dict)
        and environment.get("home") == str(root / "git-home")
        and environment.get("config_sha256") == hashlib.sha256(expected_raw).hexdigest(),
        "fresh private Git policy differs",
    )
    created: list[Path] = []
    config_path: Path | None = None
    try:
        _mkdir_new(root, created)
        home = root / "git-home"
        _mkdir_new(home, created)
        config_dir = home / ".config"
        _mkdir_new(config_dir, created)
        git_dir = config_dir / "git"
        _mkdir_new(git_dir, created)
        config_path = home / ".gitconfig"
        _write_new(config_path, expected_raw)
        import closer_coding_publication as publication

        original = {"PATH": str(gh_path.parent), "HOME": bootstrap.get("home")}
        _need(
            isinstance(original["HOME"], str) and Path(original["HOME"]).is_absolute(),
            "pinned helper home differs",
        )
        child = publication.private_git_environment_data(publication_policy, original)
        _need(
            child["HOME"] == str(home)
            and child["XDG_CONFIG_HOME"] == str(config_dir)
            and child["GIT_CONFIG_SYSTEM"] == str(config_path)
            and child["GIT_CONFIG_GLOBAL"] == str(config_path),
            "private Git environment validation differs",
        )
        return {
            "schema": "closer-fresh-private-git-environment-data/v1",
            "home": str(home),
            "config_sha256": hashlib.sha256(expected_raw).hexdigest(),
            "credential_authenticated": False,
            "execution_authority": False,
        }
    except BaseException:
        if config_path is not None:
            try:
                if config_path.is_file() and not config_path.is_symlink():
                    config_path.unlink()
            except OSError:
                pass
        for path in reversed(created):
            try:
                if path.exists() and not path.is_symlink():
                    path.rmdir()
            except OSError:
                pass
        raise
