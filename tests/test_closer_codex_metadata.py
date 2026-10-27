"""Inert metadata protocol/selection controls, not an installed account proof."""

import json

import pytest

from closer_codex_appserver_bootstrap import AccountMetadataProtocol, ProtocolHeld
from closer_codex_metadata import MetadataHeld, _argv, _source_selection, feature_values


def outgoing(protocol, method):
    value = json.loads(protocol.next_frame(now=1))
    assert value["method"] == method
    assert method not in {
        "thread/start",
        "turn/start",
        "command/exec",
        "account/login/start",
    }
    return value


def reply(protocol, message, result):
    protocol.feed((json.dumps({"id": message["id"], "result": result}) + "\n").encode(), now=1)


@pytest.mark.parametrize("ordinary", [False, True])
def test_metadata_finishes_without_thread_or_turn_and_without_billing_policy(ordinary):
    p = AccountMetadataProtocol(deadline=10)
    reply(p, outgoing(p, "initialize"), {"userAgent": "synthetic"})
    assert "id" not in outgoing(p, "initialized")
    msg = outgoing(p, "account/read")
    assert msg["params"] == {"refreshToken": False}
    reply(p, msg, {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True})
    msg = outgoing(p, "account/rateLimits/read")
    reply(p, msg, {"accountId": "synthetic-account", "ordinaryUsageAllowed": ordinary})
    result = p.snapshot()
    assert result["metadata_complete"] is True
    assert result["ready_for_source_admission"] is False
    assert result["turn_authorized"] is False
    assert result["native_thread_selection"] is None
    assert result["authority"] is False
    assert "synthetic-account" not in json.dumps(result)
    with pytest.raises(ProtocolHeld, match="final_admission_unavailable"):
        p.next_frame(now=1)


@pytest.mark.parametrize(
    "method", ["thread/started", "turn/started", "command/exec", "item/started"]
)
def test_task_or_tool_notification_refused(method):
    p = AccountMetadataProtocol(deadline=10)
    outgoing(p, "initialize")
    with pytest.raises(ProtocolHeld, match="metadata_notification_unsupported"):
        p.feed((json.dumps({"method": method, "params": {}}) + "\n").encode(), now=1)


@pytest.mark.parametrize("ordinary", [None, "false", 0])
def test_missing_or_untyped_native_account_match_does_not_count(ordinary):
    p = AccountMetadataProtocol(deadline=10)
    reply(p, outgoing(p, "initialize"), {})
    outgoing(p, "initialized")
    reply(
        p,
        outgoing(p, "account/read"),
        {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True},
    )
    with pytest.raises(ProtocolHeld, match="native_identity_match_unavailable"):
        reply(
            p,
            outgoing(p, "account/rateLimits/read"),
            {"accountId": "synthetic", "ordinaryUsageAllowed": ordinary},
        )


def test_actual_native_text_stage_spaces_and_boolean_are_parsed():
    assert feature_values(
        b"apps               stable             false\nplugins            stable             false\nfoo                under development  true\n"
    ) == {"apps": False, "plugins": False, "foo": True}


@pytest.mark.parametrize(
    "raw",
    [
        b"plugins  stable  true\n",
        b"apps  stable  false\n",
        b"plugins  stable  false\nplugins  stable  false\n",
        b'{"plugins":false}',
        b"plugins stable false\n",
        b"plugins  stable  FALSE\n",
        b"plugins  stable  false\napps  stable  true\n",
        b"plugins  stable  false\nhooks  stable  true\n",
        b"plugins  stable  false\n\x1b[31mwarning\n",
    ],
)
def test_plugins_true_missing_duplicate_malformed_or_overridden_integrations_hold(raw):
    with pytest.raises(MetadataHeld):
        feature_values(raw)


def test_actual_features_cli_does_not_receive_unsupported_strict_config():
    spec = {"binary_path": "/synthetic/codex"}
    features = _argv(spec, "features")
    assert features[-2:] == ["features", "list"]
    assert "--strict-config" not in features
    server = _argv(spec, "metadata")
    assert server[-3:] == ["app-server", "--strict-config", "--stdio"]
    assert "features.plugins=false" in features
    assert 'openai_base_url="https://chatgpt.com/backend-api/codex"' in features
    assert features[:-2] == server[:-3]


@pytest.mark.parametrize("controller", [None, {}, object()])
def test_json_or_copied_object_cannot_observe_metadata(controller):
    with pytest.raises(MetadataHeld, match="source_controller_required"):
        _source_selection(controller, {"action": "disposition"})


def test_actual_native_removed_dot_key_is_accepted_as_a_literal_feature_name():
    assert feature_values(
        b"guardianv2.thread_context  removed  false\nplugins  stable  false\n"
    ) == {
        "guardianv2.thread_context": False,
        "plugins": False,
    }


class DataCustody:
    """Data-only model of custody; never claims real queue/writer authority."""

    def __init__(self):
        self.rows = {}

    def create(self, name, value):
        if name in self.rows:
            assert self.rows[name] == value
            return False
        self.rows[name] = json.loads(json.dumps(value))
        return True

    def read(self, name):
        return json.loads(json.dumps(self.rows[name]))


def data_case(monkeypatch):
    import types

    import closer_codex_metadata as source
    import closer_correction_controller as ctl

    controller = object.__new__(ctl.IndexController)
    controller.config = types.SimpleNamespace(source_binding=lambda: {"source": "synthetic"})
    custody = DataCustody()
    operation = {"id": "synthetic-original", "deadline": 10}
    spec = {
        "schema": source.PURPOSE,
        "binary_path": "/synthetic/codex",
        "binary_sha256": "a" * 64,
        "home": "/synthetic/home",
        "codex_home": "/synthetic/codex-home",
        "private_root": "/synthetic/private",
        "cwd": "/synthetic/workspace",
        "profile_id": "synthetic-profile",
    }
    monkeypatch.setattr(source, "_source_spec", lambda current: (spec, ()))
    monkeypatch.setattr(source, "_config_pins", lambda selected: ())
    controller._require_writer = lambda op, current: None
    key = ctl.digest(
        {
            "schema": source.PURPOSE,
            "operation_id": operation["id"],
            "selection_sha256": ctl.digest(spec),
            "controller": controller.config.source_binding(),
        }
    )
    intent = {
        "schema": source.PURPOSE,
        "operation_id": operation["id"],
        "selection_sha256": ctl.digest(spec),
        "source_sha256": source._SOURCE_SHA256,
    }
    record = {
        **intent,
        "schema": "closer-codex-account-metadata-result/v1",
        "configuration_sha256": source._pin_digest(()),
        "profile_id": spec["profile_id"],
        "account_sha256": "b" * 64,
        "features_sha256": "c" * 64,
        "plugins_observed_disabled": True,
        "counted_features_bytes": 100,
        "counted_metadata_bytes": 200,
        "thread_starts": 0,
        "turn_starts": 0,
        "registration_installed": False,
        "execution_authorized": False,
        "cloud_policy_lease": False,
    }
    name = "codex-metadata-result-" + key + ".json"
    custody.create(name, record)
    custody.create("codex-metadata-intent-" + key + ".json", intent)
    reference = {
        "name": name,
        "sha256": ctl.digest(record),
        "execution_authorized": False,
    }
    bootstrap = {
        key: spec[key]
        for key in (
            "binary_path",
            "binary_sha256",
            "home",
            "codex_home",
            "private_root",
        )
    }
    return source, controller, operation, custody, reference, record, spec, bootstrap


def test_data_only_protected_result_read_and_one_time_selected_account(monkeypatch):
    source, controller, operation, custody, ref, _, spec, bootstrap = data_case(monkeypatch)
    handle = source.read_protected_account(controller, operation, custody, ref)
    assert (
        source.selected_account_sha256(
            handle,
            native_bootstrap=bootstrap,
            profile_id=spec["profile_id"],
            cwd=spec["cwd"],
        )
        == "b" * 64
    )
    with pytest.raises(MetadataHeld, match="protected_metadata_reader_required"):
        source.selected_account_sha256(
            handle,
            native_bootstrap=bootstrap,
            profile_id=spec["profile_id"],
            cwd=spec["cwd"],
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("account_sha256", "bad"),
        ("thread_starts", 1),
        ("thread_starts", False),
        ("turn_starts", 1),
        ("execution_authorized", True),
        ("registration_installed", True),
        ("cloud_policy_lease", True),
        ("plugins_observed_disabled", False),
        ("selection_sha256", "d" * 64),
        ("source_sha256", "e" * 64),
        ("profile_id", "foreign"),
        ("configuration_sha256", "f" * 64),
        ("counted_metadata_bytes", 131073),
    ],
)
def test_data_reader_rejects_changed_or_unsafe_protected_record(monkeypatch, key, value):
    import closer_correction_controller as ctl

    source, controller, operation, custody, ref, record, _, _ = data_case(monkeypatch)
    record[key] = value
    custody.rows[ref["name"]] = record
    ref["sha256"] = ctl.digest(record)
    with pytest.raises(MetadataHeld):
        source.read_protected_account(controller, operation, custody, ref)


@pytest.mark.parametrize(
    "key", ["home", "codex_home", "private_root", "binary_path", "binary_sha256"]
)
def test_data_reader_does_not_rebind_to_different_coding_selection(monkeypatch, key):
    source, controller, operation, custody, ref, _, spec, bootstrap = data_case(monkeypatch)
    handle = source.read_protected_account(controller, operation, custody, ref)
    bootstrap[key] = "foreign"
    with pytest.raises(MetadataHeld, match="initial_consumer_selection_differs"):
        source.selected_account_sha256(
            handle,
            native_bootstrap=bootstrap,
            profile_id=spec["profile_id"],
            cwd=spec["cwd"],
        )


def test_data_reader_requires_original_writer_at_consumption(monkeypatch):
    source, controller, operation, custody, ref, _, spec, bootstrap = data_case(monkeypatch)
    handle = source.read_protected_account(controller, operation, custody, ref)

    def expired(*args):
        raise MetadataHeld("original_writer_expired")

    controller._require_writer = expired
    with pytest.raises(MetadataHeld, match="original_writer_expired"):
        source.selected_account_sha256(
            handle,
            native_bootstrap=bootstrap,
            profile_id=spec["profile_id"],
            cwd=spec["cwd"],
        )


def test_data_reader_rejects_copied_json_or_forged_handle():
    import closer_codex_metadata as source

    for value in (
        {"account_sha256": "b" * 64},
        object.__new__(source.AccountQualification),
    ):
        with pytest.raises(MetadataHeld, match="protected_metadata_reader_required"):
            source.selected_account_sha256(value, native_bootstrap={}, profile_id="x", cwd="/")


def pipe_native_data(monkeypatch, *, feature_output=b"plugins  stable  false\n"):
    """Exercise actual bounded pipe mechanics without creating a subprocess."""
    import os
    import threading

    import closer_codex_metadata as source

    calls = []
    messages = []

    class PipeChild:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            self.pid, self.returncode = 987654, None
            out_read, out_write = os.pipe()
            err_read, err_write = os.pipe()
            self.stdout = os.fdopen(out_read, "rb", buffering=0)
            self.stderr = os.fdopen(err_read, "rb", buffering=0)
            os.close(err_write)
            self.stdin = None
            self.thread = None
            if argv[-2:] == ["features", "list"]:
                os.write(out_write, feature_output)
                os.close(out_write)
                self.returncode = 0
            else:
                in_read, in_write = os.pipe()
                self.stdin = os.fdopen(in_write, "wb", buffering=0)

                def reply_thread():
                    with os.fdopen(in_read, "rb", buffering=0) as stream:
                        while True:
                            raw = stream.readline()
                            if not raw:
                                break
                            msg = json.loads(raw)
                            messages.append(msg)
                            method = msg["method"]
                            assert method in {
                                "initialize",
                                "initialized",
                                "account/read",
                                "account/rateLimits/read",
                            }
                            if method == "initialized":
                                continue
                            result = {}
                            if method == "account/read":
                                result = {
                                    "account": {"type": "chatgpt"},
                                    "requiresOpenaiAuth": True,
                                }
                            if method == "account/rateLimits/read":
                                result = {
                                    "accountId": "synthetic-native",
                                    "ordinaryUsageAllowed": False,
                                }
                                # The fake PID has no OS process. Publish its
                                # terminal state before the last pipe response
                                # can cause the reader to enter cleanup.
                                self.returncode = 0
                            os.write(
                                out_write,
                                (json.dumps({"id": msg["id"], "result": result}) + "\n").encode(),
                            )
                            if method == "account/rateLimits/read":
                                break
                    self.returncode = 0
                    os.close(out_write)

                self.thread = threading.Thread(target=reply_thread)
                self.thread.start()

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            if self.thread is not None:
                self.thread.join(timeout)
                assert not self.thread.is_alive()
            assert self.returncode == 0
            return 0

    monkeypatch.setattr(source.subprocess, "Popen", PipeChild)
    return calls, messages


def test_actual_pipe_metadata_mechanics_and_intent_do_not_repeat_unknown(monkeypatch):
    import contextlib
    import time

    source, controller, operation, custody, _, _, spec, _ = data_case(monkeypatch)
    operation.update(
        id="synthetic-fresh",
        action="disposition",
        binding={source.POLICY: __import__("closer_correction_controller").digest(spec)},
        deadline=time.time() + 120,
    )
    controller.fenced = lambda op: contextlib.nullcontext(custody)
    calls, messages = pipe_native_data(monkeypatch)
    reference = source.qualify_existing_callback(controller, operation)
    result = custody.read(reference["name"])
    assert result["thread_starts"] == result["turn_starts"] == 0
    assert result["execution_authorized"] is False
    assert len(calls) == 2
    assert [msg["method"] for msg in messages] == [
        "initialize",
        "initialized",
        "account/read",
        "account/rateLimits/read",
    ]
    with pytest.raises(MetadataHeld, match="metadata_prior_intent_no_retry"):
        source.qualify_existing_callback(controller, operation)
    assert len(calls) == 2


def test_actual_features_true_pipe_refuses_before_appserver_creation(monkeypatch):
    import contextlib
    import time

    import closer_correction_controller as ctl

    source, controller, operation, custody, _, _, spec, _ = data_case(monkeypatch)
    operation.update(
        id="synthetic-fresh",
        action="disposition",
        binding={source.POLICY: ctl.digest(spec)},
        deadline=time.time() + 120,
    )
    controller.fenced = lambda op: contextlib.nullcontext(custody)
    calls, messages = pipe_native_data(monkeypatch, feature_output=b"plugins  stable  true\n")
    with pytest.raises(MetadataHeld, match="effective_plugins_not_disabled"):
        source.qualify_existing_callback(controller, operation)
    assert len(calls) == 1
    assert messages == []


def test_proxy_control_uses_actual_native_feature_namespace_not_unknown_root_key():
    from closer_codex_metadata import _environment
    from closer_codex_owned_stdio import _FIXED_OVERRIDES

    assert "respect_system_proxy=false" not in _FIXED_OVERRIDES
    assert "features.respect_system_proxy=false" in _FIXED_OVERRIDES
    assert "features.system_proxy_fallback=false" in _FIXED_OVERRIDES
    env = _environment(
        {
            "home": "/synthetic/home",
            "codex_home": "/synthetic/codex",
            "private_root": "/synthetic/private",
        }
    )
    assert not {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    } & set(env)


def remote_disabled():
    return {
        "method": "remoteControl/status/changed",
        "params": {
            "status": "disabled",
            "serverName": "synthetic-server",
            "installationId": "synthetic-installation",
            "environmentId": None,
        },
        "emittedAtMs": 12345,
    }


def test_closed_remote_disabled_notice_retains_counter_and_pending_request():
    p = AccountMetadataProtocol(deadline=10)
    request = outgoing(p, "initialize")
    p.feed((json.dumps(remote_disabled()) + "\n").encode(), now=1)
    assert p.snapshot()["notifications"] == 1
    assert p._pending[0] == request["id"]
    assert p.snapshot()["metadata_complete"] is False


@pytest.mark.parametrize(
    "path,value",
    [
        (("params", "status"), "enabled"),
        (("params", "status"), "connecting"),
        (("params", "serverName"), ""),
        (("params", "installationId"), 1),
        (("params", "environmentId"), {}),
        (("emittedAtMs",), True),
        (("emittedAtMs",), -1),
        (("emittedAtMs",), 2**63),
        (("unexpected",), True),
        (("id",), 123),
    ],
)
def test_remote_notice_cannot_hide_enabled_malformed_or_server_request(path, value):
    p = AccountMetadataProtocol(deadline=10)
    outgoing(p, "initialize")
    frame = remote_disabled()
    target = frame
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ProtocolHeld):
        p.feed((json.dumps(frame) + "\n").encode(), now=1)


def test_remote_notice_is_closed_in_coding_turn_parser_too():
    from closer_codex_appserver_bootstrap import TurnResponseData

    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    p.feed((json.dumps(remote_disabled()) + "\n").encode())
    bad = remote_disabled()
    bad["params"]["status"] = "enabled"
    with pytest.raises(ProtocolHeld, match="native_remote_control_not_disabled"):
        p.feed((json.dumps(bad) + "\n").encode())
