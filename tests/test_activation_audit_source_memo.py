"""The activation audit's two source scanners parse a given source text once per process.

`heartbeat_reachable` asks `_heartbeat_functions` and `_call_graph` about a module once for every
capability that names it, and each call re-read and re-parsed the file. One run of the admission
gate parsed the same modules 687 times (profiled 2026-10-02). The memo is keyed by the source TEXT
read on every call, so these cases pin: a repeated scan does not parse, an edit is always seen —
even one that keeps the file's size, mtime and inode — and no caller can change another's answer.

DELIBERATE BREAK -> REVERT, performed 2026-10-02: removing the memo from `_heartbeat_functions_in`
failed `test_a_repeated_scan_parses_once`. Reverted by string to a byte-identical file, green again.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import capability_activation_audit as audit

SOURCE = """# {tag}
def fires():
    production_heartbeat("cap", "invocation")


def helper():
    fires()


def idle():
    return 1
"""


class _CountingAst:
    def __init__(self):
        self.parses = 0

    def parse(self, *args, **kwargs):
        self.parses += 1
        return ast.parse(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(ast, name)


def _module(tmp_path: Path, tag: str) -> Path:
    path = tmp_path / "scanned.py"
    # The tag makes the text unique to this test, so the process-wide memo starts cold for it.
    path.write_text(SOURCE.format(tag=f"{tmp_path} {tag}"))
    return path


def test_a_repeated_scan_parses_once(tmp_path, monkeypatch):
    path = _module(tmp_path, "repeat")
    counter = _CountingAst()
    monkeypatch.setattr(audit, "ast", counter)
    for _ in range(5):
        assert audit._heartbeat_functions(path) == {"fires"}
        assert audit._call_graph(path) == {"fires": set(), "helper": {"fires"}, "idle": set()}
    assert (
        counter.parses == 2
    ), f"ten scans of one unchanged module parsed it {counter.parses} times"


def test_an_edit_that_keeps_size_mtime_and_inode_is_still_seen(tmp_path):
    path = _module(tmp_path, "edit")
    before = path.stat()
    assert audit._heartbeat_functions(path) == {"fires"}
    text = path.read_text()
    edited = text.replace("def idle():\n    return 1", "def idle():\n    fires()\n")
    assert len(edited) == len(text) and edited != text, "the edit must keep the length"
    with path.open("r+") as handle:
        handle.write(edited)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    same = (after.st_size, after.st_mtime_ns, after.st_ino)
    assert same == (before.st_size, before.st_mtime_ns, before.st_ino), "the fixture must collide"
    assert audit._call_graph(path)["idle"] == {"fires"}


def test_no_caller_can_change_another_callers_answer(tmp_path):
    path = _module(tmp_path, "private")
    names = audit._heartbeat_functions(path)
    names.add("intruder")
    graph = audit._call_graph(path)
    graph["helper"].add("intruder")
    graph["new"] = {"x"}
    assert audit._heartbeat_functions(path) == {"fires"}
    assert audit._call_graph(path) == {"fires": set(), "helper": {"fires"}, "idle": set()}


def test_an_unreadable_or_unparsable_module_scans_empty(tmp_path):
    missing = tmp_path / "missing.py"
    assert audit._heartbeat_functions(missing) == set() and audit._call_graph(missing) == {}
    broken = tmp_path / "broken.py"
    broken.write_text(f"# {tmp_path}\ndef (:\n")
    assert audit._heartbeat_functions(broken) == set() and audit._call_graph(broken) == {}
