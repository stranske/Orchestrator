"""Execute just the window default under macOS-compatible bash, never the tick body."""

import os
import subprocess

import paths


def test_window_default_expires_after_2026_11_17(tmp_path):
    text = (paths.REPO_ROOT / "orchestrate.sh").read_text()
    start = text.index("# ORCH-ANCHOR: exploration-window-start")
    snippet = text[start : text.index("# ORCH-ANCHOR: exploration-window-end", start)]
    fake_date = tmp_path / "date"
    fake_date.write_text(
        '#!/bin/bash\n[[ "$*" == "-u +%Y%m%d" ]] || exit 2\nprintf "%s\\n" "$FAKE_DATE"\n'
    )
    fake_date.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:/opt/anaconda3/bin:{os.environ['PATH']}",
        ORCH_STATE_DIR=str(tmp_path),
    )
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
