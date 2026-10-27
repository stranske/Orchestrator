"""Pure exact-block mechanics, not physical CLI or native registration proof."""

import ast
import sys
from pathlib import Path
from types import ModuleType

import pytest

import closer_campaign as campaign


@pytest.mark.parametrize("case", ["fresh", "same", "conflict", "missing", "wrong_globals"])
def test_actual_canonical_entry_block_preserves_one_module_identity(monkeypatch, case):
    tree = ast.parse(Path(campaign.__file__).read_text())
    blocks = [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Constant)
            and child.value == "campaign entry module identity unavailable"
            for child in ast.walk(node)
        )
    ]
    assert len(blocks) == 1
    main = ModuleType("__main__")
    main.__dict__["sys"] = sys
    monkeypatch.setitem(sys.modules, "__main__", main)
    monkeypatch.delitem(sys.modules, "closer_campaign", raising=False)
    if case == "same":
        monkeypatch.setitem(sys.modules, "closer_campaign", main)
    elif case == "conflict":
        monkeypatch.setitem(sys.modules, "closer_campaign", ModuleType("conflicting"))
    elif case == "missing":
        monkeypatch.delitem(sys.modules, "__main__")
    prior = sys.modules.get("closer_campaign")
    namespace = main.__dict__ if case != "wrong_globals" else {"__name__": "__main__", "sys": sys}
    code = compile(ast.Module(body=blocks, type_ignores=[]), "actual-campaign-entry-block", "exec")
    if case in {"fresh", "same"}:
        exec(code, namespace)
        assert sys.modules["closer_campaign"] is main
        assert sys.modules["closer_campaign"].__dict__ is namespace
    else:
        with pytest.raises(
            RuntimeError, match="entry module identity unavailable|conflicting canonical"
        ):
            exec(code, namespace)
        assert sys.modules.get("closer_campaign") is prior
