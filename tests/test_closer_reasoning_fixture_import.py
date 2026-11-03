"""An exported native fixture remains usable without re-exporting dependencies."""

import hashlib

from test_closer_reasoning_boundary import native_world as native_world


def test_standalone_import_constructs_only_private_inert_world(native_world):
    caller = native_world.inert_caller
    assert caller.binary == hashlib.sha256(caller.world.cli.read_bytes()).hexdigest()
    assert caller.world.events["auth"] == 0
    assert caller.world.events["started"] == []
    assert caller.world.events["ledger"] == []
    assert native_world.events["provider"] == native_world.events["effect"] == 0
