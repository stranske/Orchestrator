#!/usr/bin/env bash
# Install zsh on a CI runner without depending on Ubuntu's package servers on every run.
#
# WHY: the 15 native-shell gh-guard tests in tests/test_agent_gh_guard.py need zsh, the
# ubuntu-26.04 runner image does not ship it, and verify.py's skip ceiling (27/31 on a runner,
# 48/52 in the flat mirror shape) has no room for 15 more skips. So every job downloaded zsh
# from *.ubuntu.com. On 2026-11-11 those hosts stopped resolving from GitHub runners from about
# 14:00Z; every job failed at this step, main went red, and the mirror autodeploy held the
# merged #1344 out of the runtime for hours.
#
# NOW: the .deb files live in GitHub's Actions cache (restored by the step before this one, key
# per runner OS image family). A cache hit installs with dpkg and no network. A miss downloads
# with retries and leaves the .debs in ZSH_DEB_DIR so the step after this one saves them.
#
# Prints exactly one SOURCE line so a log read can tell which path ran:
#   SOURCE=preinstalled | SOURCE=cache | SOURCE=download(attempt N)
# and writes source=<preinstalled|cache|download> to $GITHUB_OUTPUT; the action saves the cache
# only after a download.
set -euo pipefail

emit() { [ -n "${GITHUB_OUTPUT:-}" ] && echo "source=$1" >> "$GITHUB_OUTPUT"; return 0; }

dir="${ZSH_DEB_DIR:-$HOME/.cache/ci-zsh-debs}"
attempts="${ZSH_DOWNLOAD_ATTEMPTS:-5}"

if command -v zsh >/dev/null 2>&1; then
  echo "SOURCE=preinstalled $(command -v zsh)"
  emit preinstalled
  exit 0
fi

shopt -s nullglob
cached=("$dir"/*.deb)
if ((${#cached[@]})); then
  if sudo dpkg -i "${cached[@]}" && command -v zsh >/dev/null 2>&1; then
    echo "SOURCE=cache ${#cached[@]} package(s) from $dir"
    emit cache
    exit 0
  fi
  echo "::warning::cached zsh packages in $dir did not install; downloading instead"
  # A partial dpkg -i can leave zsh unpacked but unconfigured, and apt then refuses to run on
  # the unmet dependency. Purging is offline and returns dpkg to the state before the attempt.
  sudo dpkg --purge zsh zsh-common >/dev/null 2>&1 || true
  rm -f "${cached[@]}"
fi

for ((i = 1; i <= attempts; i++)); do
  if sudo apt-get update -qq &&
    sudo apt-get install --yes --no-install-recommends --download-only zsh; then
    mkdir -p "$dir"
    cp /var/cache/apt/archives/zsh*.deb "$dir"/
    debs=("$dir"/*.deb)
    sudo dpkg -i "${debs[@]}"
    command -v zsh >/dev/null 2>&1
    echo "SOURCE=download(attempt $i) ${#debs[@]} package(s) saved to $dir"
    emit download
    exit 0
  fi
  if ((i < attempts)); then
    echo "zsh download attempt $i of $attempts failed; retrying in $((i * 20))s"
    sleep $((i * 20))
  fi
done

echo "::error::zsh unavailable: no usable cached packages in $dir and the Ubuntu package servers" \
  "failed $attempts download attempts"
exit 1
