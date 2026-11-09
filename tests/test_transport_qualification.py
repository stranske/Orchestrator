"""Retained strict final parser controls; no old Closer qualification registry."""

import builtins
import importlib.util
import sys

import pytest

import transport_qualification as q


@pytest.mark.parametrize(
    "text",
    [
        "{} trailing",
        "prose {}",
        "{}\n{}",
        "[]",
        "null",
        '{"a":1,"a":2}',
        '{"a":{"b":1,"b":2}}',
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1e999}',
        "```json\n{}\n```",
        '{"type":"tool_result"}\n{"type":"turn.completed"}',
        None,
        "x" * 65537,
    ],
)
def test_strict_final_rejects_ambiguous_or_nonfinal_payloads(text):
    with pytest.raises(ValueError):
        q.strict_final(text)


def test_saved_result_parser_imports_without_closer_registry(monkeypatch, capsys):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "closer_role_qualification":
            pytest.fail("strict final reader imported the retired qualification registry")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    spec = importlib.util.spec_from_file_location("isolated_transport_reader", q.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.strict_final('{"verdict":"PASS","evidence":{"count":2}}') == {
        "verdict": "PASS",
        "evidence": {"count": 2},
    }
    monkeypatch.setattr(sys, "argv", [q.__file__, "--selftest"])
    assert module.main() == 0
    assert "duplicate keys rejected (2 checks)" in capsys.readouterr().out
