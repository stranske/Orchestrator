"""Portable preexecution definitions for original semantic mechanisms.
Only explicit post-review selection may run the existing local INERT child.
No installed provider, account reader, network or domain ACT is used.
"""

import copy
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_closer_delivery_lifecycle as lifecycle
import test_closer_disposition as pure_disposition
from closer_semantic_fixture_support import closed_function, physical_product

import closer_evidence as evidence
import closer_pipeline

if os.environ.get("ORCH_SEMANTIC_MECHANISM_PREEXEC_REVIEWED") != "1":
    pytest.skip(
        "Original process mechanism definitions require independent preexecution review.",
        allow_module_level=True,
    )
assert os.environ.get("ORCH_REDACT_SECRET_FILES") == ""
# Process definitions are imported only after the explicit review gate.
import test_closer_execution_contract_routes as routes  # noqa: E402
from test_closer_semantic_assessment import answer  # noqa: E402

import closer_dispatcher as closer  # noqa: E402
import dispatcher  # noqa: E402

_REAL_COLLECT = evidence.collect_evidence
controlled_world = routes.controlled_world


@pytest.fixture
def physical_source_product(tmp_path):
    # All local Git input construction finishes before restrictive child ports.
    return physical_product(tmp_path / "physical-product")


@pytest.fixture(autouse=True)
def inert_transport(physical_source_product, controlled_world, monkeypatch):
    # Delegate unchanged fixture: no subprocess allowlist or lifecycle bypass.
    return routes.inert_transport.__wrapped__(controlled_world, monkeypatch)


@pytest.fixture(autouse=True)
def semantic_broker(inert_transport, physical_source_product, monkeypatch, request):
    mechanism = request.node.get_closest_marker("collection_only") is not None

    def configured(tmp_path, patch):
        product = physical_source_product
        # These are fixture constants, never production identity validators.
        patch.setattr(lifecycle, "HEAD", product.head)
        patch.setattr(pure_disposition, "HEAD", product.head)
        patch.setattr(pure_disposition, "BASE", product.base)
        delivery = routes.OfflineDelivery(tmp_path, patch)
        delivery.current.update(head=product.head, base=product.base, merge=product.head)
        delivery.supply_native_pass(patch)
        delivery.refresh()
        caller = inert_transport
        caller.activate()
        caller.seed_capacity()
        patch.setattr(closer, "_pipeline_low_route", routes._REAL_LOW)
        patch.setattr(closer, "_exception_route", routes._REAL_EXCEPTION)
        patch.setattr(closer, "_fresh_capacity_report", routes._REAL_CAPACITY)
        patch.setattr(closer, "_pipeline_capacity", lambda: {"io": 2, "low": 1, "exception": 1})
        delivery.backend.workspaces = {"owner/repo": str(product.root)}
        original_api = delivery.api

        def api(endpoint, *, method="GET", **kwargs):
            assert method == "GET", ("synthetic read-only API", method, endpoint)
            if endpoint in product.docs:
                delivery.api_calls.append((endpoint, method, kwargs))
                return copy.deepcopy(product.docs[endpoint])
            if mechanism and "/actions/runs?" in endpoint:
                return {"workflow_runs": []}
            if "/compare/" in endpoint:
                assert endpoint.endswith(product.base + "..." + product.head)
                if mechanism:
                    raise RuntimeError("synthetic exact compare data unavailable")
                return copy.deepcopy(product.diff)
            return original_api(endpoint, method=method, **kwargs)

        delivery.api = api

        def local_collect(snapshot, _api, root, **kwargs):
            # External observation DATA port only. Run the actual collector and
            # its physical/index/revision validators; never return a fixed packet.
            kwargs["artifact_fetch"] = lambda *_a, **_kw: delivery.archive
            return _REAL_COLLECT(snapshot, api, root, **kwargs)

        patch.setattr(evidence, "collect_evidence", local_collect)
        delivery.inert_caller = caller
        delivery.strong_response = lambda *_a: pytest.fail("unexpected STRONG invocation")
        delivery.physical_product = product
        delivery.semantic_calls = []

        def broker(agent, prompt, **options):
            assert not mechanism, "collection must not allocate a model child"
            assert agent == "claude"
            if options["task_type"] == "closer_interpret":
                inventory = json.loads(
                    prompt.split("CURRENT_HOST_INVENTORY_JSON_BEGIN\n", 1)[1].split(
                        "\nCURRENT_HOST_INVENTORY_JSON_END", 1
                    )[0]
                )
                output = answer(inventory)
                # Exactly one original required source question, not a finding:
                # no source-native owner/callback authority is manufactured.
                slot = inventory["required_obligation_slots"][0]
                row = next(r for r in output["obligations"] if r["slot"] == slot)
                row.update(status="unknown", gap="evidence_inconclusive")
            else:
                assert options["task_type"] == "closer_exception"
                envelope = json.loads(prompt.split("\n", 1)[1])
                req = envelope["adjudication_request"]
                supplied = envelope["supplied_evidence"][0]
                identity = next(r for r in req["proof_identities"] if r["ref"] == supplied["ref"])
                proof = copy.deepcopy(identity["alternatives"][0])
                proof.update(
                    excerpt=supplied["content"],
                    relevance="This exact supplied receipt addresses the one original synthetic criterion dispute.",
                )
                output = {
                    key: copy.deepcopy(req[key])
                    for key in (
                        "request_id",
                        "target",
                        "head",
                        "base",
                        "revision",
                        "packet_sha256",
                        "basis",
                    )
                }
                output.update(
                    schema="closer-criterion-response/v1",
                    outcome="satisfied",
                    reason="Synthetic scoped advice answers only the original named question.",
                    evidence=[supplied["ref"]],
                    proofs=[proof],
                )
            delivery.semantic_calls.append((prompt, copy.deepcopy(options), output))
            return caller.response(
                {"exit": 0, "output": json.dumps(output)}, agent, prompt, **options
            )

        patch.setattr(dispatcher, "offload", broker)
        return delivery, caller

    monkeypatch.setattr(routes, "configured_delivery", configured)


def exercise_original(tmp_path, monkeypatch, check):
    """Capture scheduling only; execute registered original callbacks in CLI scope."""
    delivery, caller = routes.configured_delivery(tmp_path, monkeypatch)
    observations = []

    def schedule(_path, _targets, **callbacks):
        source_prepare = callbacks["prepare"].__defaults__[0]
        cells = dict(
            zip(
                source_prepare.__code__.co_freevars,
                (cell.cell_contents for cell in source_prepare.__closure__),
            )
        )
        delivery.backend = cells["backend"]
        prep = callbacks["prepare"](routes.TARGET, {"deadline": time.time() + 180})
        context = dict(
            prepare=prep,
            deadline=time.time() + 180,
            revision=prep["revision"],
            identity=prep["identity"],
            work_key="synthetic-original-semantic-mechanism",
        )
        observations.append(check(delivery, callbacks, context))
        return {"targets": {routes.TARGET: {"status": "synthetic_mechanism_finished"}}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", schedule)
    caller.staged(
        SimpleNamespace(
            state=str(tmp_path / "queue.json"), max_workers=1, shadow=False, evidence_recovery=True
        ),
        [routes.TARGET],
        delivery.backend,
        time.time() + 180,
    )
    assert len(observations) == 1


def test_original_exception_and_selected_action_readback(tmp_path, monkeypatch):
    def check(delivery, cb, context):
        low = cb["interpret"](routes.TARGET, context)
        context["interpret"] = low
        original = copy.deepcopy(low)
        assert low["proposal"]["semantic_followup"]["status"] == "candidate"
        assert len(delivery.semantic_calls) == 1
        strong = cb["exception"](routes.TARGET, context)
        assert len(delivery.semantic_calls) == 2
        assert strong["validated"]["status"] == "accepted", strong
        assert strong["validated"]["scoped_adjudication"]["outcome"] == "satisfied"
        context["exception"] = strong
        context["action_state_path"] = str(tmp_path / "action-queue.json")
        original_action_backend = closed_function(cb["act"].__defaults__[0], "action_backend")
        assert original_action_backend is not None
        current = original_action_backend(routes.TARGET, context)
        assert routes.TARGET in current.dispositions
        assert low == original
        # Merely construct/read original selected-merge backend. Do not call
        # current.run_target or cb['act']; no merge/repair/domain action executes.
        assert len(delivery.semantic_calls) == 2

    exercise_original(tmp_path, monkeypatch, check)


@pytest.mark.collection_only
def test_original_necessary_collection_no_progress_and_finite_wake(tmp_path, monkeypatch):
    def check(delivery, cb, context):
        packet_ref = context["prepare"]["packet_ref"]
        packet = json.loads(Path(packet_ref["path"]).read_text())
        assert packet["sections"]["diff_files"]["status"] != "complete"
        import closer_disposition
        import closer_evidence_recovery as recovery

        snapshot = json.loads(Path(context["prepare"]["snapshot_ref"]["path"]).read_text())
        evidence_root = Path(packet_ref["path"]).parent.parent
        assert not closer_disposition._receipt_index(packet, evidence_root)
        action = context["prepare"]["proposal"]["action"]
        screen = recovery.apply_requests(
            evidence.action_evidence_readiness(
                snapshot, packet, action, retained_dir=evidence_root
            ),
            context["prepare"].get("recovery_requests", []),
            packet,
        )
        assert screen["evaluation_ready"] is False, screen
        assert recovery.required(screen)
        assert all(row["origin"] == "readiness_rail" for row in recovery.required(screen))
        first = cb["interpret"](routes.TARGET, context)
        assert first["evidence_recovery"]["outcome"] == "no_progress", first
        assert first["status"] == "pending"
        assert first["next_trigger"]
        assert not delivery.semantic_calls
        # Same actual input and source-owned Store: no guessed locator, model
        # planner, clock-only retry or acceptance can discharge the requirement.
        second = cb["interpret"](routes.TARGET, context)
        assert second["status"] == "pending"
        assert second["next_trigger"]
        assert not delivery.semantic_calls

    exercise_original(tmp_path, monkeypatch, check)
