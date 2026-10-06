#!/usr/bin/env bash
# Update VirtConvert&Create: pull the latest version and reinstall it the same way it was installed.
#   ./packaging/update.sh            (asks for your password for a system-wide installation)
# Arch package: git pull && makepkg -sif
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
if [ -d .git ]; then
  echo ">> Fetching the latest version"
  git pull --ff-only
fi
if command -v pacman >/dev/null && pacman -Q virtconvert >/dev/null 2>&1; then
  echo ">> Rebuilding the Arch package"
  exec makepkg -sif
elif command -v pacman >/dev/null && pacman -Q vertconvert >/dev/null 2>&1; then
  echo ">> Building the Arch package (it replaces the misspelt VertConvert&Create build)"
  exec makepkg -sif
elif [ -f /opt/virtconvert/.install-info ] || [ -f /opt/vertconvert/.install-info ]; then
  exec bash "$HERE/packaging/install.sh" --system --no-deps
elif [ -f "$HOME/.local/lib/virtconvert/.install-info" ] || [ -f "$HOME/.local/lib/vertconvert/.install-info" ]; then
  exec bash "$HERE/packaging/install.sh" --user --no-deps
else
  echo "No installation found; installing system-wide."
  exec bash "$HERE/packaging/install.sh"
fi
