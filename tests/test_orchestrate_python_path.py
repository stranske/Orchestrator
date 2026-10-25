"""Keep every tick's bare Python calls on the owner's standard interpreter."""

import shlex

import paths


def test_tick_path_puts_standard_python_first():
    text = (paths.REPO_ROOT / "orchestrate.sh").read_text()
    first_export = next(
        line.strip() for line in text.splitlines() if line.strip().startswith("export PATH=")
    )
    path = shlex.split(first_export)[1].removeprefix("PATH=")
    assert path.split(":")[0] == "$HOME/.codex/python-bin"
