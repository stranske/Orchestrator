"""agy-runtime-isolation exercise: every gemini command carries an ABSOLUTE --add-dir equal to the cwd; a non-gemini command carries none.
usage: check.py pass|break"""
import os, sys, tempfile
sys.path.insert(0, "src")
import adapters  # noqa: E402
mode = sys.argv[1]
with tempfile.TemporaryDirectory() as td:
    cwd = os.path.realpath(td)
    g = adapters.build_command("gemini", "say hi", cwd=cwd)
    argv = list(g["argv"] if isinstance(g, dict) and "argv" in g else g)
    flat = [str(a) for a in argv]
    if mode == "pass":
        assert "--add-dir" in flat, ("gemini command lacks --add-dir", flat[:12])
        val = flat[flat.index("--add-dir") + 1]
        assert os.path.isabs(val) and os.path.realpath(val) == cwd, ("add-dir is not the absolute cwd", val, cwd)
        print("PASS agy: gemini argv carries --add-dir", val)
    else:
        c = adapters.build_command("codex", "say hi", cwd=cwd)
        cflat = [str(a) for a in (c["argv"] if isinstance(c, dict) and "argv" in c else c)]
        assert "--add-dir" not in cflat, ("non-gemini command carries --add-dir", cflat[:12])
        print("EXPECTED_FAILURE_ABSENT: codex argv carries no --add-dir (confinement is gemini-specific)")
