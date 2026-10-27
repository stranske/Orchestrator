"""Pure byte/rejection equivalence; no native/provider/authority execution.

The original canonical oracle is retained verbatim below (name only changed).
Historical copied-evidence and whole-parent AST checks remain in their local
review receipts; this portable slice exercises no retained instance history.
"""

import ast
import hashlib
import inspect
import itertools
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_correction_controller as ctl

need = ctl.need


def original_canonical(value):
    """JS canonical byte contract, with finite safe integers and bounded trees."""
    count = 0

    def render(item, depth=0):
        nonlocal count
        count += 1
        need(depth <= 32 and count <= 50000, "private JSON structure exceeds bounds")
        if type(item) is dict:
            need(all(type(k) is str for k in item), "private JSON key is not text")
            return (
                "{"
                + ",".join(
                    render(k, depth + 1) + ":" + render(item[k], depth + 1)
                    for k in sorted(item, key=lambda k: k.encode("utf-16-be"))
                )
                + "}"
            )
        if type(item) is list:
            return "[" + ",".join(render(v, depth + 1) for v in item) + "]"
        need(
            item is None or type(item) in {str, bool, int},
            "private JSON scalar unsupported",
        )
        if type(item) is int:
            need(abs(item) <= 2**53 - 1, "private JSON integer exceeds JS safe range")
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    raw = render(value).encode("utf-8")
    need(0 < len(raw) <= 1048576, "private JSON byte bound exceeded")
    return raw


def outcome(function, value):
    try:
        return ("bytes", function(value))
    except (ValueError, TypeError, RecursionError, OverflowError) as error:
        return (type(error), str(error))


def same(value):
    expected = outcome(original_canonical, value)
    assert outcome(ctl.canonical, value) == expected
    return expected


def test_oracle_is_exact_original_function_except_name():
    tree = ast.parse(Path(__file__).read_bytes())
    node = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "original_canonical"
    )
    node.name = "canonical"
    # Python 3.13+ omits empty fields by default; retain the original structural
    # representation rather than accepting a different oracle or expected hash.
    options = {"show_empty": True} if "show_empty" in inspect.signature(ast.dump).parameters else {}
    assert hashlib.sha256(
        ast.dump(node, include_attributes=False, **options).encode()
    ).hexdigest() == ("81c250f05a3a17b4b3e563d97ee2581e6bfd90d9f3283667d2731d810d1e88e4")


def test_all_unicode_scalar_characters_in_bounded_strings_and_keys():
    # Every Unicode scalar value; each bounded chunk remains well below1MiB.
    for lower in range(0, 0x110000, 4096):
        text = "".join(
            chr(n) for n in range(lower, min(lower + 4096, 0x110000)) if not 0xD800 <= n <= 0xDFFF
        )
        assert same(text)[0] == "bytes"
        assert same({text: "value", "other": 'control\n\r\t\b\f\0"\\'})[0] == "bytes"


def test_utf16_key_order_differs_from_codepoint_order_without_changing_bytes():
    value = {"\ue000": 2, "\U00010000": 1, "a": 3}
    assert same(value) == ("bytes", '{"a":3,"𐀀":1,"\ue000":2}'.encode())
    assert same({"z": 0, "a": 1}) == same({"a": 1, "z": 0})


@pytest.mark.parametrize(
    "value",
    [
        None,
        False,
        True,
        0,
        1,
        -1,
        2**53 - 1,
        -(2**53 - 1),
        "",
        [],
        {},
        [None, False, True, "😀", 0],
        {"b": [1, 2], "a": {"z": "á"}},
    ],
)
def test_scalar_and_container_boundaries(value):
    assert same(value)[0] == "bytes"


@pytest.mark.parametrize(
    "value",
    [
        2**53,
        -(2**53),
        1.0,
        0.0,
        -0.0,
        float("nan"),
        float("inf"),
        b"bytes",
        (1,),
        {1},
        {None: "invalid"},
        {True: "invalid"},
        {1: "invalid"},
        SimpleNamespace(value=1),
        object(),
    ],
)
def test_unsupported_scalars_keys_and_safe_integer_bounds(value):
    assert same(value)[0] != "bytes"


def test_exact_types_cannot_be_broadened_to_subclasses():
    class Integer(int):
        pass

    class Text(str):
        pass

    class Array(list):
        pass

    class Object(dict):
        pass

    for value in (Integer(1), Text("a"), Array([1]), Object(a=1), {Text("a"): 1}):
        assert same(value)[0] != "bytes"


def test_malformed_unicode_surrogates_fail_identically():
    for code in range(0xD800, 0xE000):
        assert same(chr(code))[0] is UnicodeEncodeError
    for value in (
        "\ud800\udc00",
        {"a": "\udfff"},
        ["\ud800"],
        {"\ud800": 0},
        {"a": 0, "\udfff": 1},
        {"\ud800\udc00": "paired-surrogate-key"},
    ):
        assert same(value)[0] is UnicodeEncodeError


def nested(depth, kind):
    value = 0
    for _ in range(depth):
        value = [value] if kind == "list" else {"a": value}
    return value


@pytest.mark.parametrize("kind", ["list", "dict"])
def test_exact_depth_including_key_nodes(kind):
    assert same(nested(32, kind))[0] == "bytes"
    assert same(nested(33, kind)) == (
        ctl.ControllerUnknown,
        "private JSON structure exceeds bounds",
    )


def test_exact_node_count_including_dict_keys_and_shared_nodes():
    assert same([None] * 49999)[0] == "bytes"  # root+49999values
    assert same([None] * 50000) == (ctl.ControllerUnknown, "private JSON structure exceeds bounds")
    assert same({"a": [None] * 49997})[0] == "bytes"  # root,key,list+49997
    assert same({"a": [None] * 49998}) == (
        ctl.ControllerUnknown,
        "private JSON structure exceeds bounds",
    )
    shared = [None] * 100
    assert same([shared] * 495)[0] == "bytes"  # each occurrence counts independently
    assert same([shared] * 496) == (ctl.ControllerUnknown, "private JSON structure exceeds bounds")


def test_key_is_validated_before_value_at_node_limit():
    # Reversing assignment evaluation order can produce scalar instead of
    # bounds failure. The original traverses each key before its value.
    assert same([None] * 49997 + [{"k": object()}]) == (
        ctl.ControllerUnknown,
        "private JSON structure exceeds bounds",
    )
    assert same([None] * 49996 + [{"k": object()}]) == (
        ctl.ControllerUnknown,
        "private JSON scalar unsupported",
    )


def test_cycles_and_mixed_cycles_retain_bounded_refusal():
    value = []
    value.append(value)
    mapping = {}
    mapping["self"] = mapping
    mixed = []
    mapping2 = {"loop": mixed}
    mixed.append(mapping2)
    for item in (value, mapping, mixed):
        assert same(item) == (ctl.ControllerUnknown, "private JSON structure exceeds bounds")


@pytest.mark.parametrize(
    "text",
    [
        "x" * (1048576 - 2),
        "x" * (1048576 - 1),
        "😀" * ((1048576 - 2) // 4),
        "😀" * ((1048576 - 2) // 4 + 1),
        "\0" * ((1048576 - 2) // 6),
        "\0" * ((1048576 - 2) // 6 + 1),
    ],
)
def test_final_serialized_utf8_byte_bound(text):
    same(text)


def test_exhaustive_small_container_combinations():
    atoms = [None, False, True, -1, 0, 1, "", "a", "\n", '"\\', "😀", "\ue000"]
    for count in range(4):
        for values in itertools.product(atoms, repeat=count):
            assert same(list(values))[0] == "bytes"
            assert same(dict(zip(["z", "😀", "\ue000"], values)))[0] == "bytes"


def test_deterministic_mixed_tree_equivalence():
    rng = random.Random(0x7FFC)
    atoms = [None, False, True, 0, -1, 2**53 - 1, -(2**53 - 1), "", "\n\t", "😀é", "\u2028\u2029"]

    def tree(depth):
        if depth == 0 or rng.randrange(3) == 0:
            return rng.choice(atoms)
        if rng.randrange(2):
            return [tree(depth - 1) for _ in range(rng.randrange(5))]
        keys = rng.sample(["a", "z", "😀", "\ue000", "\n", "0", "é"], rng.randrange(5))
        return {k: tree(depth - 1) for k in keys}

    for _ in range(2000):
        assert same(tree(6))[0] == "bytes"


def test_one_serialization_preserves_private_input_and_exact_byte_digest(monkeypatch):
    value = {"z": [None, False, True, 1], "a": {"é": "😀"}}
    expected = original_canonical(value)
    before = json.dumps(value, ensure_ascii=False)
    original_dumps = json.dumps
    calls = []

    def measured(*args, **kwargs):
        calls.append(1)
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(ctl.json, "dumps", measured)
    assert ctl.canonical(value) == expected
    assert len(calls) == 1
    assert original_dumps(value, ensure_ascii=False) == before
    assert ctl.digest(value) == hashlib.sha256(expected).hexdigest()


def test_duplicate_and_noncanonical_decode_guards_are_unchanged():
    for raw in (b'{"a":1,"a":2}', b'{"b":2,"a":1}', b'{ "a": 1 }', b'{"a":NaN}'):
        with pytest.raises((ctl.ControllerUnknown, ValueError)):
            ctl.decode(raw)


def test_shared_aliases_are_revisited_and_next_call_reads_current_input():
    shared = {"value": [1, 2]}
    value = [shared, shared]
    before = same(value)[1]
    shared["value"].append(3)
    after = same(value)[1]
    assert before != after
    assert after == b'[{"value":[1,2,3]},{"value":[1,2,3]}]'


GOLDEN_BYTES = {
    "local-owner.json": '{"kind":"local_controller","operation_id":"inert-index-operation","operation_sha256":"1111111111111111111111111111111111111111111111111111111111111111","process_id":1234,"target":"stranske/example#7","thread_id":"12345678901234567"}\n',
    "original-seed-selector.json": '{"archive_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","artifact_id":50,"member_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","repository":"stranske/example","run_attempt":1,"run_id":700}\n',
    "local-admission-selector.json": '{"kind":"local_controller_record","owner_instruction":{"body_sha256":"7777777777777777777777777777777777777777777777777777777777777777","comment_id":801,"comment_node_id":"IC_801","issue_node_id":"I_7","issue_number":"7","repository":"stranske/example","updated_at":"2030-01-01T00:00:00Z","writer_id":99},"record_sha256":"8888888888888888888888888888888888888888888888888888888888888888","seed_selector":{"archive_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","artifact_id":50,"member_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","repository":"stranske/example","run_attempt":1,"run_id":700}}\n',
}


def test_b9_private_golden_bytes_and_opaque_owner_digest():
    expected = {
        "local-owner.json": "73713f79a67c2d1afc5843951cc3086fd7c36813f06306778fa6fbe0e7b0d5a8",
        "original-seed-selector.json": "6ce17224f30086a005941cf43f274451f82b39d695f8ed8712417ab585827046",
        "local-admission-selector.json": "f3424be493c0f5943ef80c32d05c1d9e48c9fd7bf7dcd26398ac0b7675795e75",
    }
    for name, digest in expected.items():
        raw = GOLDEN_BYTES[name].encode()
        assert raw.endswith(b"\n")
        assert ctl.canonical(json.loads(raw)) + b"\n" == raw
        assert ctl.digest(json.loads(raw)) == digest
    with pytest.raises(ctl.ControllerUnknown, match="scalar"):
        ctl.canonical({"deadline": 1.5})


def test_default_reader_cannot_upgrade_account_presence_or_policy():
    reader = ctl.NativeConsumerReader()
    with pytest.raises(ctl.ControllerUnknown, match="account reader unsupported"):
        reader.read_native_identity()
    with pytest.raises(ctl.ControllerUnknown, match="isolation unsupported"):
        reader.verify_installed_confinement(
            command=[], cwd="/tmp", env={}, custody_path=Path("/tmp")
        )
    with pytest.raises(ctl.ControllerUnknown, match="budget reader unsupported"):
        reader.read_native_budget()
