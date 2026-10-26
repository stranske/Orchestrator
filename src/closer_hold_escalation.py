"""Pinned, local delivery and receiver acknowledgment. Never confers ACT authority.

The existing controller lock serializes assignment writes. Campaign intent precedes
replacement; an interrupted replacement is reconciled only by exact byte readback.
Runtime thread attribution uses only explicit automation_runs metadata, never the
latest run or session messages. Session hashes are supplementary, not DB proof.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import inspect
import json
import math
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from types import CodeType
from typing import Any, Mapping

RECIPIENT = "reviewed-repo-stall-escalator"
MAX_BYTES = 4 * 1024 * 1024
MAX_REQUESTS = 1024


def _clock() -> float:
    now = time.time()
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("invalid escalation boundary clock")
    return now


def encoded(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: str | Path) -> bytes:
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("escalation input exceeds bounded size")
    return data


def atomic(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".escalation-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _path(value: Any) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("route paths must be absolute")
    path = Path(value)
    if path != path.resolve():
        raise ValueError("route path must be canonical")
    return path


def _pin(ref: Any) -> Path:
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise ValueError("missing exact source pin")
    path = _path(ref["path"])
    if sha(read(path)) != ref["sha256"]:
        raise ValueError("escalation source pin changed")
    return path


def route(doc: Mapping[str, Any], manifest: Mapping[str, Any], manifest_path: str) -> dict:
    if manifest.get("package_digest") != doc.get("release_pin"):
        raise ValueError("escalation package differs from campaign pin")
    configured = (doc.get("worker_config") or {}).get("runtime_manifest")
    if configured is not None and _path(configured) != _path(manifest_path):
        raise ValueError("foreign campaign runtime manifest")
    if json.loads(read(manifest_path)) != manifest:
        raise ValueError("manifest differs from retained source")
    value = (manifest.get("mechanism_contract") or {}).get("hold_escalation_route")
    if not value or value != (doc.get("worker_config") or {}).get("hold_escalation_route"):
        raise ValueError("explicit pinned escalation route unavailable")
    if (
        value.get("schema") != "closer-hold-escalation-route/v1"
        or value.get("recipient") != RECIPIENT
    ):
        raise ValueError("unregistered escalation recipient")
    root = _path(value["controller_root"])
    receiver = _path(value["receiver_root"])
    if root.name != "reviewed-repo-automation-controller" or receiver.name != RECIPIENT:
        raise ValueError("foreign controller/receiver")
    if _path(value["destination"]) != root / "escalator-assignment.json":
        raise ValueError("foreign assignment destination")
    if _pin(value["controller_source"]) != root / "demand_router.py":
        raise ValueError("foreign controller source")
    for phase, filename in (("preflight", "preflight.py"), ("terminal", "record_run.py")):
        if _pin(value["receiver_sources"][phase]) != receiver / filename:
            raise ValueError("foreign receiver source")
    if _pin(value["consumer_source"]) != Path(__file__).resolve():
        raise ValueError("foreign escalation consumer")
    _path(value["app_db"])
    _path(value["receiver_source_cwd"])
    window = value.get("ack_window_seconds")
    if type(window) not in (int, float) or not math.isfinite(window) or not 1 <= window <= 86400:
        raise ValueError("explicit bounded acknowledgment window required")
    return copy.deepcopy(value)


def _bound(doc: dict, request: dict) -> None:
    from closer_hold_followup import _assert_bound

    hold = doc.get("hold_followups", {}).get(request["hold_id"])
    if (
        not hold
        or hold.get("status") != "escalated"
        or sha(encoded(hold)) != request["hold_sha256"]
    ):
        raise ValueError("original escalated hold changed")
    _assert_bound(doc, hold)
    for key in (
        "campaign_id",
        "owner",
        "generation",
        "release_pin",
        "config_sha256",
        "target",
        "revision",
        "run_id",
        "execution_state",
    ):
        if request.get(key) != hold[key]:
            raise ValueError("escalation request differs from original hold")
    if (
        request.get("operation_id") != hold["source"].get("operation_id")
        or request.get("authority") is not False
    ):
        raise ValueError("escalation operation/authority differs from original hold")


def _row(request_id: str, row: dict, policy: dict | None = None) -> None:
    request = row["request"]
    if (
        request.get("schema") != "closer-hold-escalation-request/v1"
        or sha(encoded(request)) != request_id
        or row.get("delivery_operation_id") != sha(encoded([request_id, "assignment"]))
    ):
        raise ValueError("escalation request/delivery identity changed")
    if policy is not None and (
        request.get("recipient") != policy["recipient"]
        or request.get("destination") != policy["destination"]
        or request.get("route_sha256") != sha(encoded(policy))
        or request.get("ack_deadline")
        != request.get("created_at", 0) + policy["ack_window_seconds"]
    ):
        raise ValueError("escalation request route/window changed")


def _delivery(campaign: Any, request_id: str, row: dict, policy: dict) -> None:
    """Authenticate the original readback, independently of current formatting."""
    receipt = row.get("delivery_receipt")
    expected_keys = {"operation_id", "request_id", "payload_sha256", "destination", "readback_at"}
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        raise ValueError("missing exact escalation delivery receipt")
    payload_sha = row.get("payload_sha256")
    if not isinstance(payload_sha, str) or len(payload_sha) != 64:
        raise ValueError("invalid retained delivery hash")
    path = _path(row.get("payload_ref"))
    expected_path = (
        campaign.path.parent / (campaign.path.name + ".escalations") / (payload_sha + ".json")
    ).resolve()
    if path != expected_path:
        raise ValueError("foreign retained delivery payload")
    original = read(path)
    if (
        sha(original) != payload_sha
        or json.loads(original).get("closer_hold_escalations", {}).get(request_id) != row["request"]
    ):
        raise ValueError("original delivery payload/request mismatch")
    readback_at = receipt["readback_at"]
    if (
        receipt["operation_id"] != row["delivery_operation_id"]
        or receipt["request_id"] != request_id
        or receipt["destination"] != policy["destination"]
        or receipt["payload_sha256"] != payload_sha
        or type(readback_at) not in (int, float)
        or not math.isfinite(readback_at)
        or not row["request"]["created_at"] <= readback_at <= row["request"]["ack_deadline"]
        or readback_at > time.time()
    ):
        raise ValueError("delivery receipt differs from original operation/readback")


def _request(doc: dict, hold: dict, policy: dict, now: float) -> dict:
    from closer_hold_followup import _assert_bound, validate_hold

    if not validate_hold(hold):
        raise ValueError("invalid escalation hold")
    _assert_bound(doc, hold)
    return {
        "schema": "closer-hold-escalation-request/v1",
        "hold_id": hold["hold_id"],
        "hold_sha256": sha(encoded(hold)),
        **{
            key: hold[key]
            for key in (
                "campaign_id",
                "owner",
                "generation",
                "release_pin",
                "config_sha256",
                "target",
                "revision",
                "run_id",
                "execution_state",
            )
        },
        "operation_id": hold["source"].get("operation_id"),
        "recipient": policy["recipient"],
        "destination": policy["destination"],
        "route_sha256": sha(encoded(policy)),
        "created_at": now,
        "ack_deadline": now + policy["ack_window_seconds"],
        "authority": False,
    }


def _finish(campaign: Any, request_id: str, expected: dict, **fields: Any) -> dict:
    def update(doc):
        row = doc["hold_escalations"][request_id]
        _row(request_id, row)
        _bound(doc, row["request"])
        if row != expected:
            raise ValueError("escalation changed concurrently")
        if fields.get("status") == "outcome_unknown" and _clock() > row["request"]["ack_deadline"]:
            row.update(status="ack_overdue")
        else:
            row.update(fields)

    return campaign.update(update)


def _complete_delivery(
    campaign: Any, request_id: str, expected: dict, policy: dict, actual: bytes
) -> dict:
    def update(doc):
        row = doc["hold_escalations"][request_id]
        _row(request_id, row, policy)
        _bound(doc, row["request"])
        if row != expected:
            raise ValueError("escalation changed before delivery readback")
        boundary = _clock()
        row.update(
            status="ack_overdue" if boundary > row["request"]["ack_deadline"] else "delivered",
            delivery_receipt={
                "operation_id": row["delivery_operation_id"],
                "request_id": request_id,
                "payload_sha256": sha(actual),
                "destination": policy["destination"],
                "readback_at": boundary,
            },
        )

    return campaign.update(update)


def service(
    campaign: Any, manifest: Mapping[str, Any], manifest_path: str, now: float | None = None
) -> tuple[dict, list[dict]]:
    """One bounded local pass; no provider, workflow, or ACT API is reachable."""
    current = time.time() if now is None else now
    if type(current) not in (int, float) or not math.isfinite(current):
        raise ValueError("invalid escalation clock")
    doc = campaign.read()
    if not doc:
        raise ValueError("no campaign")
    # Empty work is inert even on hosts without escalation configuration. Validate
    # both journals before proving emptiness; malformed/unknown rows are not empty.
    from closer_hold_followup import validate_hold

    holds = doc.get("hold_followups", {})
    journal = doc.get("hold_escalations", {})
    if not isinstance(holds, dict) or not isinstance(journal, dict):
        raise ValueError("malformed escalation journals")
    if len(holds) > MAX_REQUESTS or len(journal) > MAX_REQUESTS:
        raise ValueError("escalation journal exceeds bound")
    for hold_id, hold in holds.items():
        if (
            not isinstance(hold_id, str)
            or not validate_hold(hold)
            or hold.get("hold_id") != hold_id
        ):
            raise ValueError("malformed escalation hold journal")
    pending = False
    for request_id, row in journal.items():
        if (
            not isinstance(request_id, str)
            or not isinstance(row, dict)
            or not isinstance(row.get("request"), dict)
        ):
            raise ValueError("malformed escalation request journal")
        _row(request_id, row)
        if row.get("status") not in {
            "confirmed_not_started",
            "outcome_unknown",
            "delivered",
            "acknowledged",
            "ack_overdue",
        }:
            raise ValueError("unknown escalation request status")
        pending |= row["status"] not in {"acknowledged", "ack_overdue"}
    if not pending and not any(hold["status"] == "escalated" for hold in holds.values()):
        return doc, []
    reports = []
    deliveries = 0
    try:
        policy = route(doc, manifest, manifest_path)
        if _path(manifest["hold_escalation_campaign_path"]) != campaign.path.resolve():
            raise ValueError("foreign escalation campaign path")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return doc, [{"kind": "escalation_unrouted", "reason": str(exc), "authority": False}]
    for request_id, row in journal.items():
        if row["status"] not in {"acknowledged", "ack_overdue"}:
            _row(request_id, row, policy)
            _bound(doc, row["request"])
    for hold_id, hold in sorted(holds.items()):
        if hold.get("status") != "escalated":
            continue
        claimed: dict[str, str] = {}

        def claim(value):
            # The original read only selects work. Claim/reuse is authoritative
            # under Campaign's lock, including another controller's newer row.
            route(value, manifest, manifest_path)
            current_journal = value.setdefault("hold_escalations", {})
            if len(current_journal) > MAX_REQUESTS:
                raise ValueError("escalation journal exceeds bound")
            existing = [
                (key, candidate)
                for key, candidate in current_journal.items()
                if candidate["request"]["hold_id"] == hold_id
            ]
            if len(existing) > 1:
                raise ValueError("ambiguous escalation request")
            if existing:
                key, candidate = existing[0]
                _row(key, candidate, policy)
                _bound(value, candidate["request"])
            else:
                if len(current_journal) >= MAX_REQUESTS:
                    raise ValueError("escalation journal exceeds bound")
                if value.get("hold_followups", {}).get(hold_id) != hold:
                    raise ValueError("hold changed before escalation claim")
                request = _request(value, hold, policy, current)
                key = sha(encoded(request))
                current_journal[key] = {
                    "request": request,
                    "status": "confirmed_not_started",
                    "delivery_operation_id": sha(encoded([key, "assignment"])),
                }
            claimed["request_id"] = key

        doc = campaign.update(claim)
        request_id = claimed["request_id"]
        # No Campaign lock is held while acquiring the controller lock. Every
        # effect decision below uses the CURRENT claimed state under demand.lock.
        with (Path(policy["controller_root"]) / ".demand.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            doc = campaign.read()
            route(doc, manifest, manifest_path)
            row = copy.deepcopy(doc["hold_escalations"][request_id])
            _row(request_id, row, policy)
            _bound(doc, row["request"])
            if row["status"] in {"acknowledged", "ack_overdue"}:
                continue
            if _clock() > row["request"]["ack_deadline"]:
                doc = _finish(campaign, request_id, row, status="ack_overdue")
                reports.append(
                    {
                        "kind": "escalation_ack_overdue",
                        "request_id": request_id,
                        "authority": False,
                    }
                )
                continue
            if row["status"] == "delivered":
                _delivery(campaign, request_id, row, policy)
                continue
            if deliveries >= 2:
                continue
            deliveries += 1
            destination = Path(policy["destination"])
            if row["status"] == "outcome_unknown":
                original = read(_path(row["payload_ref"]))
                if sha(original) != row["payload_sha256"]:
                    raise ValueError("retained delivery payload changed")
                actual = read(destination)
                if actual != original:
                    reports.append(
                        {
                            "kind": "escalation_delivery_unknown",
                            "request_id": request_id,
                            "authority": False,
                        }
                    )
                    continue
            else:
                assignment = json.loads(read(destination))
                requests = assignment.setdefault("closer_hold_escalations", {})
                if request_id in requests and requests[request_id] != row["request"]:
                    raise ValueError("conflicting assignment request")
                requests[request_id] = row["request"]
                payload = encoded(assignment)
                if len(payload) > MAX_BYTES:
                    raise ValueError("assignment exceeds bound")
                directory = campaign.path.parent / (campaign.path.name + ".escalations")
                directory.mkdir(exist_ok=True)
                payload_path = (directory / (sha(payload) + ".json")).resolve()
                try:
                    with payload_path.open("xb") as retained:
                        retained.write(payload)
                        retained.flush()
                        os.fsync(retained.fileno())
                except FileExistsError:
                    if read(payload_path) != payload:
                        raise ValueError("retained payload conflicts")
                doc = _finish(
                    campaign,
                    request_id,
                    row,
                    status="outcome_unknown",
                    payload_sha256=sha(payload),
                    payload_ref=str(payload_path),
                )
                row = doc["hold_escalations"][request_id]
                if row["status"] == "ack_overdue" or _clock() > row["request"]["ack_deadline"]:
                    if row["status"] != "ack_overdue":
                        doc = _finish(campaign, request_id, row, status="ack_overdue")
                    reports.append(
                        {
                            "kind": "escalation_ack_overdue",
                            "request_id": request_id,
                            "authority": False,
                        }
                    )
                    continue
                # Any interruption from this boundary remains UNKNOWN. Never infer
                # no-write from an old destination hash (ABA is possible).
                atomic(destination, payload)
                actual = read(destination)
                if actual != payload:
                    raise ValueError("assignment readback mismatch")
            doc = _complete_delivery(campaign, request_id, row, policy, actual)
            status = doc["hold_escalations"][request_id]["status"]
            reports.append(
                {
                    "kind": (
                        "escalation_delivered"
                        if status == "delivered"
                        else "escalation_ack_overdue"
                    ),
                    "request_id": request_id,
                    "authority": False,
                }
            )
    return doc, reports


def _codes(code: CodeType):
    yield code
    for value in code.co_consts:
        if isinstance(value, CodeType):
            yield from _codes(value)


def receiver_context(manifest_path: str, source_path: str) -> tuple[Any, dict, dict, dict]:
    """Authenticate a pinned running receiver; no caller-supplied actor identity."""
    from closer_campaign import Campaign

    manifest = json.loads(read(manifest_path))
    campaign = Campaign(manifest["hold_escalation_campaign_path"])
    doc = campaign.read()
    if doc is None:
        raise ValueError("no campaign")
    policy = route(doc, manifest, manifest_path)
    source = _path(source_path)
    if source not in {_path(ref["path"]) for ref in policy["receiver_sources"].values()}:
        raise ValueError("foreign receiver source")
    expected = tuple(_codes(compile(read(source), str(source), "exec", dont_inherit=True)))
    frame = inspect.currentframe()
    caller = frame.f_back if frame else None
    if caller is not None and caller.f_code is receiver_ack.__code__:
        caller = caller.f_back
    if (
        caller is None
        or caller.f_code.co_name != "main"
        or caller.f_code.co_filename != str(source)
        or caller.f_code not in expected
    ):
        raise ValueError("acknowledgment requires actual pinned receiver main")
    return campaign, manifest, policy, _runtime_actor(policy, source)


def _runtime_actor(policy: dict, source: Path) -> dict:
    thread = os.environ.get("CODEX_THREAD_ID")
    session = os.environ.get("CODEX_SESSION_ID")
    if not thread or not session:
        raise ValueError("missing runtime receiver context")
    with sqlite3.connect(_path(policy["app_db"]).as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT thread_id,automation_id,status,source_cwd,created_at,updated_at FROM automation_runs WHERE thread_id=?",
            (thread,),
        ).fetchmany(2)
    if len(rows) != 1:
        raise ValueError("absent or ambiguous exact runtime run")
    actor = dict(rows[0])
    now = _clock()
    if (
        actor["automation_id"] != policy["recipient"]
        or actor["status"] not in {"IN_PROGRESS", "RUNNING"}
        or actor["source_cwd"] != policy["receiver_source_cwd"]
        or str(Path.cwd().resolve()) != policy["receiver_source_cwd"]
        or type(actor["created_at"]) is not int
        or not 0 <= now - actor["created_at"] / 1000 <= policy["ack_window_seconds"]
    ):
        raise ValueError("foreign or stale runtime receiver run")
    actor.update(
        session_context_sha256=sha(session.encode()),
        receiver_source={"path": str(source), "sha256": sha(read(source))},
    )
    return actor


def receiver_ack(manifest_path: str, source_path: str) -> list[str]:
    """Consume delivery only; source hold and unknown-effect barriers stay intact."""
    campaign, manifest, policy, actor = receiver_context(manifest_path, source_path)
    acknowledged = []
    with (Path(policy["controller_root"]) / ".demand.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assignment_bytes = read(policy["destination"])
        assignment = json.loads(assignment_bytes)
        doc = campaign.read()
        for request_id, row in doc.get("hold_escalations", {}).items():
            if row["status"] not in {"delivered", "acknowledged"}:
                continue
            _row(request_id, row, policy)
            request = row["request"]
            _bound(doc, request)
            _delivery(campaign, request_id, row, policy)
            if assignment.get("closer_hold_escalations", {}).get(request_id) != request:
                continue
            if row["status"] == "acknowledged":
                continue
            accepted = []

            def acknowledge(value):
                current_row = value["hold_escalations"][request_id]
                route(value, manifest, manifest_path)
                _row(request_id, current_row, policy)
                _bound(value, request)
                if current_row != row:
                    raise ValueError("escalation changed before acknowledgment")
                _delivery(campaign, request_id, current_row, policy)
                if _clock() > request["ack_deadline"]:
                    return  # Expiration never backdates receipt or grants authority.
                current_actor = _runtime_actor(policy, _path(source_path))
                boundary = _clock()
                if (
                    not request["created_at"]
                    <= current_actor["created_at"] / 1000
                    <= boundary
                    <= request["ack_deadline"]
                ):
                    return
                current_row.update(
                    status="acknowledged",
                    acknowledged_at=boundary,
                    observed_assignment_sha256=sha(assignment_bytes),
                    acknowledgment={
                        "schema": "closer-hold-escalation-ack/v1",
                        "request_id": request_id,
                        "request": request,
                        "delivery_receipt": current_row["delivery_receipt"],
                        "actor": current_actor,
                        "authority": False,
                    },
                )
                accepted.append(request_id)

            doc = campaign.update(acknowledge)
            acknowledged.extend(accepted)
    return acknowledged


def render_receiver(
    source: str, phase: str, manifest_path: str, consumer_path: str, consumer_sha256: str
) -> str:
    """Render a disposable copy only. Caller must separately pin the final bytes."""
    loader = (
        "    import hashlib as _eh, importlib.util as _ei\n"
        f"    _ep = {consumer_path!r}\n"
        f"    if _eh.sha256(Path(_ep).read_bytes()).hexdigest() != {consumer_sha256!r}: raise ValueError('escalation consumer pin changed')\n"
        "    _es = _ei.spec_from_file_location('_closer_escalation_receiver', _ep)\n"
        "    _em = _ei.module_from_spec(_es); _es.loader.exec_module(_em)\n"
        f"    _ec = _em.receiver_context({manifest_path!r}, __file__)\n"
        f"    _em.receiver_ack({manifest_path!r}, __file__)\n"
    )
    if phase == "preflight":
        start = source.index("    try:\n        with sqlite3.connect(")
        end = source.index("    with concurrent.futures.", start)
        return source[:start] + loader + '    result["current_run"] = _ec[3]\n' + source[end:]
    if phase == "terminal":
        old = "    thread_id, created_at_ms = current_run(args.app_db)\n"
        if source.count(old) != 1:
            raise ValueError("receiver terminal source shape changed")
        return source.replace(
            old,
            loader + '    thread_id, created_at_ms = _ec[3]["thread_id"], _ec[3]["created_at"]\n',
        )
    raise ValueError("unknown receiver phase")


def preserve_assignment(manifest_path: str, controller_root: str, assignment: dict) -> dict:
    """Carry exact outstanding delivery across the existing controller writer.

    Called while its existing demand lock is held; does not acquire another lock.
    """
    from closer_campaign import Campaign

    manifest = json.loads(read(manifest_path))
    campaign = Campaign(manifest["hold_escalation_campaign_path"])
    doc = campaign.read()
    if doc is None:
        raise ValueError("no campaign")
    policy = route(doc, manifest, manifest_path)
    if controller_root != policy["controller_root"]:
        raise ValueError("foreign controller assignment writer")
    result = copy.deepcopy(assignment)
    retained = {}
    for key, row in doc.get("hold_escalations", {}).items():
        if row["status"] in {"delivered", "outcome_unknown"}:
            _row(key, row, policy)
            _bound(doc, row["request"])
            if row["status"] == "delivered":
                _delivery(campaign, key, row, policy)
            if row["status"] == "outcome_unknown":
                # The ordinary writer must not turn an unconfirmed intent into
                # a fresh delivery. Only preserve an already exact readback.
                original = read(_path(row["payload_ref"]))
                actual = read(policy["destination"])
                if sha(original) != row["payload_sha256"] or actual != original:
                    continue
                # Settle the original exact write before this ordinary writer
                # changes assignment formatting or unrelated discovery fields.
                _complete_delivery(campaign, key, row, policy, actual)
                if _clock() > row["request"]["ack_deadline"]:
                    continue
            retained[key] = row["request"]
    if retained:
        result["closer_hold_escalations"] = retained
    return result


def render_controller(
    source: str, manifest_path: str, consumer_path: str, consumer_sha256: str
) -> str:
    """Wrap only the canonical lane assignment writer in a disposable copy."""
    old = 'save(ROOT / (lane + "-assignment.json"), {'
    if source.count(old) != 1:
        raise ValueError("controller assignment writer source shape changed")
    helper = (
        "\ndef _escalation_save(path, value):\n"
        "    import hashlib as _eh, importlib.util as _ei\n"
        f"    _ep = {consumer_path!r}\n"
        f"    if _eh.sha256(Path(_ep).read_bytes()).hexdigest() != {consumer_sha256!r}: raise ValueError('escalation consumer pin changed')\n"
        "    _es = _ei.spec_from_file_location('_closer_escalation_controller', _ep)\n"
        "    _em = _ei.module_from_spec(_es); _es.loader.exec_module(_em)\n"
        "    if path == ROOT / 'escalator-assignment.json':\n"
        f"        value = _em.preserve_assignment({manifest_path!r}, str(ROOT), value)\n"
        "    return save(path, value)\n"
    )
    # Append before the existing __main__ invocation so ordinary main resolves it.
    boundary = source.index('if __name__ == "__main__":')
    return (
        source[:boundary].replace(old, '_escalation_save(ROOT / (lane + "-assignment.json"), {')
        + helper
        + source[boundary:]
    )
