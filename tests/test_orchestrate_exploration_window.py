"""Execute just the window default under macOS-compatible bash, never the tick body."""

import os
import subprocess

import paths


def test_window_default_expires_after_2026_11_17(tmp_path):
    text = (paths.REPO_ROOT / "orchestrate.sh").read_text()
    start = text.index('if [[ "$(date -u +%Y%m%d)"')
    snippet = text[start : text.index("\nfi", start) + 3]
    fake_date = tmp_path / "date"
    fake_date.write_text(
        '#!/bin/bash\n[[ "$*" == "-u +%Y%m%d" ]] || exit 2\nprintf "%s\\n" "$FAKE_DATE"\n'
    )
    fake_date.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    env.pop("ORCH_EXPLORATION_RATE", None)
    for day, expected in [("20261017", "0.20"), ("20261117", "0.20"), ("20261118", "unset")]:
        result = subprocess.run(
            ["/bin/bash", "-uc", snippet + '\nprintf "%s" "${ORCH_EXPLORATION_RATE-unset}"'],
            env=dict(env, FAKE_DATE=day),
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout == expected
        for override in ("0", "0.35"):
            result = subprocess.run(
                ["/bin/bash", "-uc", snippet + '\nprintf "%s" "$ORCH_EXPLORATION_RATE"'],
                env=dict(env, FAKE_DATE=day, ORCH_EXPLORATION_RATE=override),
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout == override
