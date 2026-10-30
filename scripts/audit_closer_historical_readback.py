"""Audit the literal historical readback against explicitly supplied local paths.

This never runs workers, providers, plans, or effects. Corrupt data is injected
only into the privately staged diagnostic child; original evidence is read-only.
The retained, authenticated case is intentionally not a portable CI fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path

WORKER = "59dfb3c05e55"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "old-manifest", "old-state", "interpreter"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source))
    import closer_native_source_upgrade as upgrade

    result = Path(str(args.old_state) + ".results") / (WORKER + ".json")
    manifest = json.loads(args.old_manifest.read_text())
    payload = json.loads(result.read_text())
    context = payload["native_admission"]
    detail = json.loads(payload["detail"])
    directory = Path(manifest["mechanism_contract"]["observation_root"]) / context["nonce"]
    round_id = detail["completed_round"]["round_id"]
    paths = [
        args.old_manifest,
        args.old_state,
        result,
        directory / "admission.json",
        directory / "assignment.json",
        directory / "scoped-capacity.json",
        directory / ("worker-" + WORKER + ".json"),
        directory / ("completed-cohort-" + WORKER + ".json"),
        directory / ("completed-round-" + round_id + ".json"),
        Path(detail["completed_round"]["receipt_path"]),
    ]

    def hashes():
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

    before = hashes()
    original = upgrade._historical_clock_reader_program

    @contextmanager
    def fault(code):
        def factory():
            marker = "from closer_dispatcher import targets_from_assignment\n"
            program = original()
            assert program.count(marker) == 1
            return program.replace(marker, marker + code + "\n", 1)

        upgrade._historical_clock_reader_program = factory
        try:
            yield
        finally:
            upgrade._historical_clock_reader_program = original

    def diagnostic(worker=WORKER):
        return upgrade._old_diagnostic(
            args.old_manifest, args.old_state, worker, interpreter=args.interpreter
        )

    def run(name, code, *, worker=WORKER, qualification=False):
        with fault(code):
            saved = upgrade.subprocess.run
            seen = {}

            def capture(*args, **kwargs):
                value = saved(*args, **kwargs)
                seen["exit"] = value.returncode
                return value

            upgrade.subprocess.run = capture
            try:
                answer = diagnostic(worker)
                if qualification:
                    assert answer["strong_as_of"] is False
                    upgrade._diagnostic_no_act(upgrade.REVIEWED_CLOCK_CANDIDATE, answer)
            except upgrade.SourceUpgradeHeld as exc:
                return f"exit={seen.get('exit')}; {exc}"
            finally:
                upgrade.subprocess.run = saved
        raise AssertionError(name + " accepted")

    def read_mutation(path, expression):
        return (
            "_original_read=n._read\n"
            "def _corrupted_read(p):\n"
            " value=_original_read(p)\n"
            f" if str(p)=={str(path)!r}:\n"
            f"  value=json.loads(json.dumps(value)); {expression}\n"
            " return value\n"
            "n._read=_corrupted_read"
        )

    cases = {
        "result": (
            "_original_cohort_read=c._cohort_read\n"
            "def _corrupted_cohort_read(p,*args):\n"
            " raw=_original_cohort_read(p,*args)\n"
            f" return raw+b' ' if str(p)=={str(result)!r} else raw\n"
            "c._cohort_read=_corrupted_cohort_read",
            "exit=58;",
        ),
        "admission_signature": (
            read_mutation(directory / "admission.json", "value['signature']='0'*64"),
            "exit=40;",
        ),
        "admission_binding": (
            read_mutation(directory / "admission.json", "value['record']['binding']={}"),
            "exit=40;",
        ),
        "completed_round": (
            read_mutation(
                directory / ("completed-round-" + round_id + ".json"),
                "value['record']['proof']['accepted']=True",
            ),
            "exit=53;",
        ),
        "wrong_worker": ("pass", "exit=38;"),
    }
    failures = {}
    for name, (code, prefix) in cases.items():
        worker = "0" * 12 if name == "wrong_worker" else WORKER
        failures[name] = run(name, code, worker=worker)
        assert failures[name].startswith(prefix), (name, failures[name])

    # Replace only registry INPUT DATA. The real qualified_record validator,
    # execution-record validator, proof validation, and final no-ACT gate run.
    # No wall clock or validator result is patched, and no registry is written.
    registry_prefix = (
        "import closer_role_qualification as _roles\n"
        "_original_qualified_record=_roles.qualified_record\n"
        "def _qualified_from_corrupted_input(profile,role,*,now=None,records=None):\n"
        " registry=copy.deepcopy(_roles.ROLE_QUALIFICATIONS)\n"
    )
    registry_suffix = (
        " return _original_qualified_record(profile,role,now=now,records=registry)\n"
        "_roles.qualified_record=_qualified_from_corrupted_input"
    )
    qualification_cases = {
        "missing_qualification": " registry={}\n",
        "expired_before_admission": (
            " for value in registry.values():\n"
            f"  value['expires_at']=min(value['expires_at'],{context['started_at']!r}-1)\n"
        ),
        "different_completion_record": (
            f" if now!={context['started_at']!r}:\n"
            "  registry={key+'-corrupted':dict(value,qualification_id=key+'-corrupted') "
            "for key,value in registry.items()}\n"
        ),
    }
    for name, mutation in qualification_cases.items():
        failures[name] = run(name, registry_prefix + mutation + registry_suffix, qualification=True)
        assert failures[name].startswith("exit=0;"), (name, failures[name])
        assert "STRONG historical qualification" in failures[name], (name, failures[name])

    positive = diagnostic()
    assert positive["strong_as_of"] is True
    upgrade._diagnostic_no_act(upgrade.REVIEWED_CLOCK_CANDIDATE, positive)
    after = hashes()
    assert before == after, "original evidence changed during audit"
    print(
        json.dumps(
            {"positive": True, "failures": failures, "hashes": after, "unchanged": True},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
