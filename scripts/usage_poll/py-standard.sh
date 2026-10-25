# Sourced by scheduled scripts: sets STANDARD_PYTHON = the owner's standard interpreter.
# Owner standard (2026-10-19): Python 3.14 first, 3.13 as backup. Never the macOS system 3.9 and never
# the anaconda 3.12 base. Override with ORCH_STANDARD_PYTHON (must still report 3.13 or 3.14).
# Prints a refusal and returns 1 when none is usable (never falls back to another python3).
standard_python_candidates() {
  local c
  for c in "${ORCH_STANDARD_PYTHON:-}" \
           "$HOME"/.local/share/uv/python/cpython-3.14*/bin/python3.14 \
           /opt/homebrew/bin/python3.14 /usr/local/bin/python3.14 \
           /opt/homebrew/bin/python3.13 /usr/local/bin/python3.13 \
           "$HOME"/.local/share/uv/python/cpython-3.13*/bin/python3.13; do
    [[ -n "$c" && -x "$c" ]] || continue
    case "$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" in
      3.14|3.13) printf '%s\n' "$c"; return 0 ;;
    esac
  done
  return 1
}
STANDARD_PYTHON="$(standard_python_candidates)" || {
  printf 'standard python unavailable: need Python 3.14 (backup 3.13); refusing system/anaconda fallback\n' >&2
  return 1 2>/dev/null || exit 1
}
export STANDARD_PYTHON
