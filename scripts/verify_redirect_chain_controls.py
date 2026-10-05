import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile

w = pathlib.Path(__file__).resolve().parents[1]
controls = [
    (
        "keepalive_shadow.py",
        "test_an_escalated_pr_is_not_live_and_is_eligible",
        'state = "escalated"',
        'state = "running"',
    ),
    (
        "redirect_apply.py",
        "test_a_sweep_stall_proposal_reaches_the_apply_candidate_list",
        "for cand in sweep:",
        "for cand in []:",
    ),
]
rows = []
for module, test, anchor, broken in controls:
    with tempfile.TemporaryDirectory(prefix="closer425-control-") as name:
        temp = pathlib.Path(name)
        source = (w / "src" / module).read_text()
        assert source.count(anchor) == 1, (module, source.count(anchor))
        f = temp / module
        f.write_text(source)
        runs = []
        for phase in ["baseline", "broken", "restored"]:
            f.write_text(source.replace(anchor, broken) if phase == "broken" else source)
            r = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "pytest",
                    str(w / "tests/test_redirect_chain_candidates.py") + "::" + test,
                    "-q",
                    "-o",
                    "addopts=",
                    "-o",
                    "pythonpath=" + str(temp) + " " + str(w / "src") + " " + str(w / "tests"),
                ],
                cwd=w,
                text=True,
                capture_output=True,
            )
            assert r.returncode == (1 if phase == "broken" else 0), (phase, r.stdout, r.stderr)
            assert (
                test in r.stdout and "AssertionError" in r.stdout
                if phase == "broken"
                else "1 passed" in r.stdout
            )
            runs.append(dict(phase=phase, exit=r.returncode, output=r.stdout + r.stderr))
        rows.append(
            dict(
                module=module,
                test=test,
                source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                runs=runs,
            )
        )
print(json.dumps(rows, indent=2))
