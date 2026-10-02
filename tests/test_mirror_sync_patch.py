from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import paths

DOC = paths.REPO_ROOT / "docs" / "MIRROR_SYNC_PATCH.md"


def _documented_patch() -> str:
    text = DOC.read_text(encoding="utf-8")
    heading = text.index("**The patch.**")
    start = text.index("```bash\n", heading) + len("```bash\n")
    end = text.index("\n```", start)
    return text[start:end]


@pytest.fixture()
def wrapper_world(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    source = tmp_path / "source with spaces"
    mirror = tmp_path / "live mirror"
    staged = tmp_path / "staged payload"
    temp = tmp_path / "tmp"
    for path in (home / ".codex" / "bin", source / "scripts", mirror, staged / "scripts", temp):
        path.mkdir(parents=True, exist_ok=True)

    (source / "payload.txt").write_text("mutable source\n")
    (staged / "payload.txt").write_text("verified bytes\n")
    fake_installer = staged / "scripts" / "install_verified_snapshot.py"
    fake_installer.write_text(
        "import os, pathlib, shutil, sys\n"
        "snapshot, mirror = map(pathlib.Path, sys.argv[1:3])\n"
        "mirror.mkdir(parents=True, exist_ok=True)\n"
        "shutil.copy2(snapshot / 'payload.txt', mirror / 'payload.txt')\n"
        "pathlib.Path(os.environ['FAKE_RECORD']).open('a').write('install\\n')\n"
    )

    pre = source / "scripts" / "verify_before_sync.sh"
    pre.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        '[[ "$1" == --snapshot-out ]]\n'
        'out="$2"\n'
        'src="$3"\n'
        '[[ "${FAKE_PRE_RC:-0}" == 0 ]] || exit "$FAKE_PRE_RC"\n'
        'cp -R "$FAKE_SNAPSHOT" "$out"\n'
        "printf 'changed after verdict\\n' > \"$src/payload.txt\"\n"
    )
    pre.chmod(0o755)

    ordinary = home / ".codex" / "bin" / "orch-sync-mirror.sh"
    ordinary.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf 'ordinary-copy\\n' >> \"$FAKE_RECORD\"\n"
        'cp "$1/payload.txt" "$FAKE_MIRROR/payload.txt"\n'
    )
    ordinary.chmod(0o755)

    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "RUN_VERIFY=${RUN_VERIFY:-1}\n"
        "ALLOW_UNMERGED=${ALLOW_UNMERGED:-0}\n"
        "SRC=$FAKE_SRC\n"
        "MIRROR=$FAKE_MIRROR\n" + _documented_patch() + "\n"
    )
    wrapper.chmod(0o755)
    return {
        "home": home,
        "source": source,
        "mirror": mirror,
        "staged": staged,
        "temp": temp,
        "record": tmp_path / "record.txt",
        "wrapper": wrapper,
        "pre": pre,
    }


def _run(world: dict[str, Path], **env: str) -> subprocess.CompletedProcess:
    base = {
        "PATH": os.environ["PATH"],
        "HOME": str(world["home"]),
        "TMPDIR": str(world["temp"]),
        "FAKE_SRC": str(world["source"]),
        "FAKE_MIRROR": str(world["mirror"]),
        "FAKE_SNAPSHOT": str(world["staged"]),
        "FAKE_RECORD": str(world["record"]),
    }
    return subprocess.run(
        ["bash", str(world["wrapper"])],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_green_path_installs_the_retained_snapshot_not_mutated_source(wrapper_world):
    result = _run(wrapper_world)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (wrapper_world["source"] / "payload.txt").read_text() == "changed after verdict\n"
    assert (wrapper_world["mirror"] / "payload.txt").read_text() == "verified bytes\n"
    assert wrapper_world["record"].read_text().splitlines() == ["install"]
    assert list(wrapper_world["temp"].iterdir()) == []


def test_red_and_missing_preverifier_fail_closed_before_any_copy(wrapper_world):
    red = _run(wrapper_world, FAKE_PRE_RC="7")
    assert red.returncode == 3, red.stdout + red.stderr
    assert "NOT SYNCED" in red.stderr
    assert not wrapper_world["record"].exists()
    assert not (wrapper_world["mirror"] / "payload.txt").exists()

    wrapper_world["pre"].unlink()
    missing = _run(wrapper_world)
    assert missing.returncode == 3, missing.stdout + missing.stderr
    assert "no scripts/verify_before_sync.sh" in missing.stderr
    assert "source\\ with\\ spaces" in missing.stderr
    assert not wrapper_world["record"].exists()


def test_no_verify_is_the_only_path_that_invokes_the_ordinary_copier(wrapper_world):
    wrapper_world["pre"].unlink()
    result = _run(wrapper_world, RUN_VERIFY="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert wrapper_world["record"].read_text().splitlines() == ["ordinary-copy"]
    assert (wrapper_world["mirror"] / "payload.txt").read_text() == "mutable source\n"
    assert "copy is NOT a verdict" in result.stdout
