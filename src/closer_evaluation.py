"""Read-only historical-input evaluation driver; never a queue or effect receipt.

Actual provider accounting and shared pool ownership stay in dispatcher.offload.
Only optional stage-quality telemetry is retained locally. Historical material is
untrusted assessment context and cannot discharge uncertainty or admit actions.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024
SCHEMA = "closer-historical-evaluation-batch/v1"
_ACTIVE = contextvars.ContextVar("closer_evaluation", default=None)


class EvaluationHeld(ValueError):
    pass


def _raw(path):
    path = Path(path)
    if (
        not path.is_absolute()
        or str(path.resolve()) != str(path)
        or any(p.is_symlink() for p in (path, *path.parents))
    ):
        raise EvaluationHeld("evaluation input must be canonical and unaliased")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
            raise EvaluationHeld("evaluation input is not a bounded regular file")
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_nlink",
    )
    if len(raw) > MAX_BYTES or any(
        getattr(before, k) != getattr(after, k) or getattr(after, k) != getattr(path.stat(), k)
        for k in fields
    ):
        raise EvaluationHeld("evaluation input changed during read")
    return raw


def read_ref(ref):
    if (
        not isinstance(ref, dict)
        or not isinstance(ref.get("path"), str)
        or not isinstance(ref.get("sha256"), str)
    ):
        raise EvaluationHeld("pinned evaluation reference missing")
    raw = _raw(ref["path"])
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise EvaluationHeld("evaluation reference changed")
    try:
        return json.loads(
            raw, parse_constant=lambda _x: (_ for _ in ()).throw(ValueError("nonfinite JSON"))
        )
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise EvaluationHeld("evaluation reference is not bounded valid JSON") from exc


def _write(root, name, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    if len(raw) > MAX_BYTES:
        raise EvaluationHeld("evaluation output exceeds retention cap")
    path = root / name
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb"
    ) as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def validate_args(args):
    if not getattr(args, "evaluate_only", False):
        raise EvaluationHeld("explicit evaluation opt-in missing")
    for flag in (
        "apply",
        "workers",
        "pipeline",
        "shadow",
        "dispositions",
        "finite_frontier_json",
        "coordinator_contract_pin",
    ):
        if getattr(args, flag, None):
            raise EvaluationHeld("evaluation forbids --" + flag.replace("_", "-"))
    if getattr(args, "coordinator_mode", "legacy") != "legacy":
        raise EvaluationHeld("evaluation cannot enter a production coordinator")
    if not getattr(args, "runtime_manifest", None):
        raise EvaluationHeld("evaluation requires pinned qualified runtime manifest")
    if (
        not getattr(args, "evaluation_output", None)
        or not getattr(args, "evaluation_batch", None)
        or not getattr(args, "evaluation_batch_sha256", None)
    ):
        raise EvaluationHeld("fresh output and pinned batch are required")
    timeout = args.request_timeout_seconds
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 5 <= timeout <= 120:
        raise EvaluationHeld("evaluation request timeout must be 5..120 seconds")
    root = Path(args.evaluation_output)
    if (
        not root.is_absolute()
        or str(root.resolve()) != str(root)
        or root.exists()
        or root.is_symlink()
    ):
        raise EvaluationHeld("evaluation output must be a fresh canonical directory")
    if any(
        part in {".git", ".codex", ".claude", ".cursor", ".aws", ".agents"} for part in root.parts
    ):
        raise EvaluationHeld("evaluation output enters a protected control/source directory")
    if any(p.is_symlink() for p in root.parents) or not root.parent.is_dir():
        raise EvaluationHeld("evaluation output parent is not canonical/existing")
    manifest_parent = Path(args.runtime_manifest).parent
    if root.is_relative_to(manifest_parent):
        raise EvaluationHeld("evaluation output cannot modify the runtime package")
    implementation = Path(__file__).resolve().parent
    source_root = implementation.parent if implementation.name == "src" else implementation
    if root.is_relative_to(source_root):
        raise EvaluationHeld("evaluation output cannot modify executing implementation")
    original = Path(args.state)
    if any(
        parent.parent == original.parent
        and (parent.name == original.name or parent.name.startswith(original.name + "."))
        for parent in (root, *root.parents)
    ):
        raise EvaluationHeld("evaluation output intersects original operational state family")
    return root


def _source_target(original, raw):
    import closer_quarantine as q

    def identity(value):
        if not isinstance(value, str):
            raise EvaluationHeld("original source identity missing")
        match = re.fullmatch(
            r"https://github\.com/([^/]+/[^/]+)/(?:issues|pull)/([1-9][0-9]*)", value
        )
        return q._target(match[1] + "#" + match[2] if match else value)

    expected = identity(original.get("source_target") or original.get("source"))
    if not isinstance(raw, dict) or identity(raw.get("target") or raw.get("html_url")) != expected:
        raise EvaluationHeld("pinned raw source identity differs")
    if "number" in raw and (
        type(raw["number"]) is not int or raw["number"] != int(expected.rsplit("#", 1)[1])
    ):
        raise EvaluationHeld("pinned raw source number differs")
    return expected


def _source_rows(rows, expected, body):
    import closer_quarantine as q

    if not isinstance(rows, list) or len(rows) > 128:
        raise EvaluationHeld("original source inventory unavailable")
    matching = [
        row for row in rows if isinstance(row, dict) and q._target(row.get("target")) == expected
    ]
    if len(matching) != 1 or matching[0].get("body") != body:
        raise EvaluationHeld("original source identity/body differs from collected sources")


def validate_prepared_sources(prep, item):
    original = item["original_task"]
    expected = _source_target(original, read_ref(original["raw_ref"]))
    snapshot = read_ref(prep["snapshot_ref"])
    packet = read_ref(prep["packet_ref"])
    _source_rows(snapshot.get("sources"), expected, original["body"])
    _source_rows(
        packet.get("sections", {}).get("source", {}).get("content"), expected, original["body"]
    )


def _historical_row(doc, record, target):
    import closer_quarantine as q
    from closer_pipeline import SCHEMA as PIPELINE_SCHEMA
    from closer_queue import SCHEMA as QUEUE_SCHEMA

    locator = record.get("locator")
    if not isinstance(locator, list) or "run_id" not in record:
        raise EvaluationHeld("historical exact locator/run identity missing")
    schema = doc.get("schema") if isinstance(doc, dict) else None
    if (
        schema == PIPELINE_SCHEMA
        and len(locator) == 4
        and locator[0] == "runs"
        and locator[2] == "targets"
        and isinstance(locator[1], str)
        and record["run_id"] == locator[1]
    ):
        pass
    elif (
        schema == PIPELINE_SCHEMA
        and len(locator) == 2
        and locator[0] == "carry"
        and record["run_id"] == "carry"
    ):
        pass
    elif (
        schema == QUEUE_SCHEMA
        and len(locator) == 2
        and locator[0] == "items"
        and record["run_id"] in (None, "native_queue")
    ):
        pass
    else:
        raise EvaluationHeld("unsupported historical source/run locator")
    if q._target(locator[-1]) != q._target(target):
        raise EvaluationHeld("foreign historical target locator")
    try:
        row = doc
        for key in locator:
            row = row[key]
    except (KeyError, TypeError, IndexError) as exc:
        raise EvaluationHeld("historical exact row locator unavailable") from exc
    if (
        not isinstance(row, dict)
        or row != record.get("row")
        or ("target" in row and q._target(row["target"]) != q._target(target))
    ):
        raise EvaluationHeld("historical exact row changed or has foreign target")


def load_batch(args, targets):
    import closer_dispatcher as d
    import closer_quarantine as q

    batch_ref = {"path": args.evaluation_batch, "sha256": args.evaluation_batch_sha256}
    batch = read_ref(batch_ref)
    if (
        not isinstance(batch, dict)
        or batch.get("schema") != SCHEMA
        or batch.get("mutating_authority") is not False
    ):
        raise EvaluationHeld("evaluation batch has wrong schema/authority")
    items = batch.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        raise EvaluationHeld("evaluation batch must name 1..8 immutable items")
    if any(not isinstance(item, dict) for item in items):
        raise EvaluationHeld("evaluation items must be complete objects")
    selected = [q._target(item.get("target")) for item in items]
    if len(set(selected)) != len(selected) or selected != [q._target(t) for t in targets]:
        raise EvaluationHeld("evaluation batch and assignment targets differ")
    history = batch.get("canonical_history_unchanged")
    required = {str(Path(args.state)), str(d._pipeline_state_path(args.state))}
    if not isinstance(history, dict) or not required.issubset(history):
        raise EvaluationHeld("original raw/pipeline history pins missing")
    for path, sha in history.items():
        read_ref({"path": path, "sha256": sha})
    output = Path(args.evaluation_output)
    protected = [Path.home() / name for name in (".codex", ".claude", ".cursor", ".vibe")]
    protected += [
        Path(str(args.state) + suffix)
        for suffix in (".evidence", ".pipeline.json.scoped", ".shadow.pipeline.json.scoped")
    ]
    if any(output == path or output.is_relative_to(path) for path in protected):
        raise EvaluationHeld("evaluation output intersects protected operational roots")
    for item in items:
        observation = read_ref(item.get("observation_ref"))
        if q._target(observation.get("target")) != q._target(item["target"]) or observation.get(
            "revision"
        ) != item.get("current_revision"):
            raise EvaluationHeld("batch current observation differs")
        original = item.get("original_task")
        if (
            not isinstance(original, dict)
            or not isinstance(original.get("body"), str)
            or not original["body"].strip()
        ):
            raise EvaluationHeld("original source criteria missing")
        original_raw = read_ref(original.get("raw_ref"))
        if not isinstance(original_raw, dict) or original_raw.get("body") != original["body"]:
            raise EvaluationHeld("original source body differs from pinned raw source")
        expected_source = _source_target(original, original_raw)
        _source_rows(observation.get("sources"), expected_source, original["body"])
        material = read_ref(item.get("historical_material_ref"))
        if material.get("schema") != "closer-historical-material/v1" or q._target(
            material.get("target")
        ) != q._target(item["target"]):
            raise EvaluationHeld("historical material target differs")
        for ref in material.get("source_refs", []):
            if history.get(ref.get("path")) != ref.get("sha256"):
                raise EvaluationHeld("historical source outside pinned original inputs")
            read_ref(ref)
        for record in material.get("records", []):
            source = record.get("source") or {}
            if history.get(source.get("path")) != source.get("sha256"):
                raise EvaluationHeld("historical row outside pinned original inputs")
            value = read_ref(source)
            _historical_row(value, record, item["target"])
        for ref in item.get("available_evidence", []):
            read_ref(ref)
    return batch, batch_ref


def active():
    value = _ACTIVE.get()
    if value is None:
        raise EvaluationHeld("no active trusted evaluation callback context")
    return value


def _runtime(backend, manifest_ref):
    from closer_runtime_binding import binding_from_manifest, policy_for_mechanism

    manifest = read_ref(manifest_ref)
    try:
        binding = binding_from_manifest(manifest_ref["path"])
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        raise EvaluationHeld(
            f"runtime qualification held: {type(exc).__name__}: {str(exc)[:500]}"
        ) from exc
    if binding is None:
        raise EvaluationHeld("qualified runtime binding unavailable")
    problem = policy_for_mechanism(binding.policy, mechanism=binding.mechanism)
    if problem:
        raise EvaluationHeld(problem)
    backend.runtime_binding = binding
    return {
        "candidate_sha": manifest.get("candidate_sha"),
        "package_digest": manifest.get("package_digest"),
        "portable": manifest.get("portable"),
    }


def _source_pins():
    return {
        str(path): hashlib.sha256(_raw(path)).hexdigest()
        for path in (
            Path(__file__).resolve(),
            Path(__file__).with_name("closer_dispatcher.py").resolve(),
        )
    }


def before_provider(context):
    """Final read-only guard and durable once marker, before actual offload."""
    value = active()
    if value.get("phase") != "interpret" or context.get("finite_frontier") != value.get("frontier"):
        raise EvaluationHeld("provider outside exact interpretation stage")
    if _source_pins() != value["source_pins"]:
        raise EvaluationHeld("evaluation adapter source changed before provider")
    load_batch(value["args"], value["targets"])
    validate_prepared_sources(value["prepare"], value["item"])
    _runtime(value["backend"], value["runtime_ref"])
    deadline = context.get("deadline")
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= time.time():
        raise EvaluationHeld("original evaluation request window expired")
    _write(
        value["root"],
        f"{value['index']}-provider-started.json",
        {
            "target": value["item"]["target"],
            "deadline": deadline,
            "frontier": value["frontier"],
            "authority": False,
            "created_at": time.time(),
        },
    )
    if deadline <= time.time():
        raise EvaluationHeld("evaluation deadline expired retaining request marker")


def _callbacks(prepare, interpret):
    caller = sys._getframe(2)
    module = sys.modules.get(getattr(prepare, "__module__", ""))
    entry = getattr(module, "_run_staged_pipeline", None)
    expected = Path(__file__).with_name("closer_dispatcher.py").resolve()
    if (
        module is None
        or getattr(module, "__name__", None) not in {"closer_dispatcher", "__main__"}
        or Path(getattr(module, "__file__", "")).resolve() != expected
        or entry is None
        or caller.f_code is not entry.__code__
    ):
        raise EvaluationHeld("evaluation driver requires the production dispatcher entry")
    for fn, name in ((prepare, "prepare"), (interpret, "interpret")):
        if (
            fn.__module__ != module.__name__
            or fn.__name__ != name
            or fn.__code__ not in entry.__code__.co_consts
        ):
            raise EvaluationHeld("foreign evaluation callback")


def run(args, targets, backend, *, prepare, interpret, bind_frontier):
    """Trusted production closures only; invoke each selected stage once, no resume."""
    import closer_dispatcher as d
    import closer_frontier_policy as f
    import closer_quarantine as q
    import closer_resources as resources

    _callbacks(prepare, interpret)
    root = validate_args(args)
    batch, batch_ref = load_batch(args, targets)
    if backend.apply or backend.workers or backend.dispositions:
        raise EvaluationHeld("effect-enabled evaluation backend forbidden")
    runtime_ref = {
        "path": str(Path(args.runtime_manifest)),
        "sha256": hashlib.sha256(_raw(args.runtime_manifest)).hexdigest(),
    }
    runtime_pins = _runtime(backend, runtime_ref)
    source_pins = _source_pins()
    census = q.scope_snapshot(d._pipeline_state_path(args.state))
    if census.get("global_reasons"):
        raise EvaluationHeld("historical census unreadable; evaluation held")
    # Do not call scope_problem, mint admission, subtract keys, or reinterpret old
    # fences as permission. The census is untrusted assessment input, kept whole.
    os.mkdir(root, 0o700)
    parent_fd = os.open(root.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    census_ref = _write(root, "historical-census.json", census)
    result = {
        "schema": "closer-historical-evaluation-result/v1",
        "authority": False,
        "accepted": False,
        "effect_dispatches": 0,
        "batch_ref": batch_ref,
        "adapter_source_sha256": source_pins,
        "qualified_runtime_ref": runtime_ref,
        "qualified_runtime_pins": runtime_pins,
        "historical_census_ref": census_ref,
        "items": [],
    }
    _write(root, "evaluation-intent.json", {**result, "created_at": time.time()})

    def forbid(*_a, **_kw):
        raise EvaluationHeld("evaluation cannot invoke an effect/worker/reconciliation callback")

    for name in ("execute", "worker", "reconcile", "close_sources", "initiate_verify"):
        setattr(backend, name, forbid)
    for index, (target, item) in enumerate(zip(targets, batch["items"], strict=True)):
        row = {"target": target, "authority": False, "accepted": False, "status": "held"}
        result["items"].append(row)
        state = {
            "root": root,
            "item": item,
            "batch_ref": batch_ref,
            "census_ref": census_ref,
            "feedback_seq": 0,
            "index": index,
            "args": args,
            "targets": targets,
            "backend": backend,
            "runtime_ref": runtime_ref,
            "source_pins": source_pins,
            "phase": "prepare",
        }

        def feedback_writer(*a, **kw):
            state["feedback_seq"] += 1
            _write(
                root,
                f"{index}-stage-feedback-{state['feedback_seq']}.json",
                {"args": a, "kwargs": kw, "authority": False},
            )

        state["feedback_writer"] = feedback_writer
        token = _ACTIVE.set(state)
        try:
            load_batch(args, targets)
            if _source_pins() != source_pins:
                raise EvaluationHeld("evaluation adapter source changed")
            _runtime(backend, runtime_ref)
            _write(
                root,
                f"{index}-prepare-started.json",
                {"target": target, "at": time.time(), "authority": False},
            )
            context = {
                "deadline": time.time() + args.request_timeout_seconds,
                "state_path": str(root / "evaluation.json"),
                "action_state_path": str(root / "never-created-actions.json"),
            }
            with resources.inventory_request(context["deadline"]):
                prep = prepare(target, context)
            row["prepare_ref"] = _write(root, f"{index}-prepare.json", prep)
            validate_prepared_sources(prep, item)
            state["prepare"] = prep
            if (
                prep.get("revision") != item["current_revision"]
                or (prep.get("snapshot") or {}).get("target") != target
            ):
                raise EvaluationHeld("fresh preparation changed pinned target/revision")
            # Build transport ownership only after the fresh prepared revision is
            # checked. This descriptor is not a pipeline/action admission.
            observation = read_ref(item["observation_ref"])
            operation_binding = prep["evaluation_operation_binding"]
            if operation_binding != item["new_closer_decision"]["operation_binding"] or (
                prep.get("proposal") or {}
            ).get("action") != item["new_closer_decision"].get("action"):
                raise EvaluationHeld("fresh selection/action binding changed pinned batch")
            frontier = f.make_frontier(
                [target],
                owner="historical-evaluation",
                frontier_id="evaluation-"
                + hashlib.sha256((batch_ref["sha256"] + str(root) + target).encode()).hexdigest()[
                    :32
                ],
                bindings={
                    target: {
                        "revision": prep["revision"],
                        "action_id": f.action_identity(
                            target,
                            prep["revision"],
                            (prep.get("proposal") or {}).get("action"),
                            operation_binding,
                        ),
                    }
                },
            )
            if observation.get("revision") != prep["revision"]:
                raise EvaluationHeld("evaluation observation changed")
            bind_frontier(frontier)
            state["frontier"] = frontier
            state["phase"] = "interpret"
            load_batch(args, targets)
            if _source_pins() != source_pins:
                raise EvaluationHeld("evaluation adapter source changed")
            _runtime(backend, runtime_ref)
            if d.assignment_dependency(
                args.assignment, target, expected_fingerprint=prep.get("assignment_fingerprint")
            ):
                raise EvaluationHeld("current assignment held or changed")
            _write(
                root,
                f"{index}-interpret-started.json",
                {"target": target, "at": time.time(), "frontier": frontier, "authority": False},
            )
            context = {
                **context,
                "prepare": prep,
                "revision": prep["revision"],
                "identity": prep.get("identity"),
                "finite_frontier": frontier,
                "deadline": time.time() + args.request_timeout_seconds,
            }
            with resources.inventory_request(context["deadline"]):
                interpreted = interpret(target, context)
            row["interpret_ref"] = _write(root, f"{index}-interpret.json", interpreted)
            row["status"] = (
                "held"
                if interpreted.get("status") in {"uncertain", "pending", "deferred"}
                else "assessment_returned"
            )
            row["next_action"] = (
                "senior_review_needed"
                if interpreted.get("substantive_needs")
                else "review_advisory_result"
            )
            row["classification"] = interpreted.get("classification")
            row["actual"] = interpreted.get("actual") or (
                interpreted.get("provider_outcome") or {}
            ).get("actual")
            load_batch(args, targets)
        except Exception as exc:
            row["status"] = "held"
            row["reason"] = f"{type(exc).__name__}: {str(exc)[:1000]}"
        finally:
            bind_frontier(None)
            _ACTIVE.reset(token)
    result["receipt_ref"] = _write(root, "evaluation-result.json", result)
    return result
