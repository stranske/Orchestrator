"""Dedicated Muse terminal/usage DATA parser for the unwired runtime component.

No account, quota, execution or role authority from usage metadata. Keeping this
parser separate preserves the qualified production adapter's exact source bytes.
"""

import json


def muse_terminal_text(jsonl: str) -> str:
    """Read the observed ``muse exec --json`` durable run-terminal protocol.

    A completed task or progress message is not a completed run. Bind the terminal to the
    command/session whose acceptance, run linkage and start were observed. Unknown, malformed,
    truncated or conflicting streams fail closed. Native timestamps are not accounting clocks.
    """
    command = session = None
    linked = started = False
    terminals: list[str] = []
    # Observed native exec event vocabulary. A new lifecycle kind needs explicit review;
    # silently accepting it could hide a failure or a second run after completion.
    auxiliary = {
        "run.model.configured",
        "run.output.delta",
        "turn.input.user",
        "task.stream.linked",
        "task.lifecycle.accepted",
        "task.lifecycle.completed",
        "task.lifecycle.proposed",
        "task.lifecycle.scheduled",
        "task.lifecycle.side_effect_intent",
        "task.lifecycle.started",
        "task.lifecycle.status",
        "task.lifecycle.output",
        "task.lifecycle.failed",
        "task.lifecycle.rejected",
        "tool.result",
    }
    for line in str(jsonl or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            return ""
        if not isinstance(event, dict):
            return ""
        if any(
            type(event.get(key)) is not int or event[key] != 1
            for key in ("schema_version", "payload_schema_version")
        ):
            return ""
        payload = event.get("payload")
        stream = event.get("stream")
        if not isinstance(payload, dict) or not isinstance(stream, dict):
            return ""
        kind = event.get("payload_type", "")
        if not isinstance(kind, str) or terminals:
            return ""
        if kind not in auxiliary | {
            "runtime.command.accepted",
            "session.run.linked",
            "run.lifecycle.started",
            "run.terminal.completed",
            "run.usage",
        }:
            return ""
        if kind == "runtime.command.accepted":
            if (
                command is not None
                or payload.get("kind") != "command_accepted"
                or event.get("durability") != "durable"
            ):
                return ""
            command, session = payload.get("command_id"), stream.get("id")
            if (
                not isinstance(command, str)
                or not command
                or not isinstance(session, str)
                or not session
                or payload.get("command_kind") != "turn.submit"
            ):
                return ""
        if command is None:
            return ""
        if kind == "run.usage":
            # Installed exec emits this distinct, ephemeral metadata stream.
            # It is not a run terminal, budget proof or authorization assertion.
            counters = {"calls", "completed_calls", "failed_calls", "cancelled_calls"}
            model_counters = {
                "input_tokens",
                "cached_tokens",
                "output_tokens",
                "reasoning_tokens",
                "calls",
            }

            def counter(value):
                return type(value) is int and 0 <= value <= 2**64 - 1

            def allowance(value):
                # Native RunUsageAllowance is typed metadata only. Neither a
                # remaining percentage nor a reset timestamp grants account,
                # billing, execution or subscription authority here.
                if value is None:
                    return True
                if type(value) is not dict or set(value) != {
                    "tier",
                    "window",
                    "weekly",
                    "as_of_ms",
                    "source",
                }:
                    return False
                tier = value["tier"]
                if not (tier is None or type(tier) is str and 0 < len(tier.strip()) <= 128):
                    return False
                if (
                    type(value["as_of_ms"]) is not int
                    or not 0 <= value["as_of_ms"] <= 2**63 - 1
                    or type(value["source"]) is not str
                    or value["source"] not in {"stream", "mint"}
                ):
                    return False
                for period in (value["window"], value["weekly"]):
                    if period is None:
                        continue
                    if (
                        type(period) is not dict
                        or set(period) != {"used_percent", "resets_at_ms"}
                        or type(period["used_percent"]) is not int
                        or not 0 <= period["used_percent"] <= 100
                    ):
                        return False
                    reset = period["resets_at_ms"]
                    if reset is not None and (
                        type(reset) is not int or not 0 <= reset <= 2**63 - 1
                    ):
                        return False
                return True

            models = payload.get("models")
            if (
                not started
                or stream != {"kind": "exec.usage", "id": session}
                or event.get("causation_id") != command
                or event.get("record_type") != "status"
                or event.get("durability") != "ephemeral"
                or set(payload)
                != counters
                | {"kind", "command_id", "run_stream", "models", "children_included", "allowance"}
                or payload.get("kind") != "run_usage"
                or payload.get("command_id") != command
                or payload.get("run_stream") != {"kind": "run", "id": command}
                or type(payload.get("children_included")) is not bool
                or not allowance(payload.get("allowance"))
                or not all(counter(payload[key]) for key in counters)
                or type(models) is not list
                or len(models) > 128
            ):
                return ""
            for model in models:
                if (
                    type(model) is not dict
                    or set(model) != model_counters | {"model"}
                    or not (model["model"] is None or type(model["model"]) is str)
                    or not all(counter(model[key]) for key in model_counters)
                ):
                    return ""
            continue
        if (
            stream != {"kind": "session", "id": session}
            or event.get("causation_id") != command
            or event.get("durability") not in {"durable", "ephemeral"}
        ):
            return ""
        if kind in {"session.run.linked", "run.lifecycle.started"} or kind.startswith(
            "run.terminal."
        ):
            if (
                event.get("durability") != "durable"
                or payload.get("command_id") != command
                or payload.get("run_stream") != {"kind": "run", "id": command}
            ):
                return ""
        # Native tool-bearing runs emit these task outcomes before their one run
        # terminal. A denied/failed child tool can still produce a useful completed
        # review; never promote child output or failure into the run verdict.
        if kind in {
            "task.lifecycle.output",
            "task.lifecycle.failed",
            "task.lifecycle.rejected",
            "tool.result",
        }:
            expected_kind = "tool_result" if kind == "tool.result" else "task_lifecycle"
            if (
                event.get("durability") != "durable"
                or event.get("record_type") != "event"
                or payload.get("kind") != expected_kind
                or payload.get("command_id") != command
                or payload.get("run_stream") != {"kind": "run", "id": command}
            ):
                return ""
            if kind == "tool.result":
                if (
                    not isinstance(payload.get("call_id"), str)
                    or not payload["call_id"]
                    or not isinstance(payload.get("text"), str)
                ):
                    return ""
            else:
                task = payload.get("task_id")
                child = payload.get("event")
                if (
                    not isinstance(task, str)
                    or not task
                    or payload.get("task_stream") != {"kind": "task", "id": task}
                    or not isinstance(child, dict)
                    or child.get("kind") != kind.rsplit(".", 1)[-1]
                    or child.get("task_id") != task
                ):
                    return ""
        if kind == "session.run.linked":
            if linked or payload.get("kind") != "session_run_linked":
                return ""
            linked = True
        elif kind == "run.lifecycle.started":
            if not linked or started or payload.get("kind") != "run_started":
                return ""
            started = True
        elif kind.startswith("run.terminal."):
            if (
                not started
                or event.get("record_type") != "event"
                or kind != "run.terminal.completed"
                or payload.get("kind") != "run_terminal"
                or payload.get("terminal") != "completed"
                or payload.get("reason") is not None
            ):
                return ""
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return ""
            terminals.append(text.strip())
    return terminals[0] if len(terminals) == 1 else ""
