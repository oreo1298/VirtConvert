#!/usr/bin/env bash
# Remove an installation made by packaging/install.sh (also one of VirtConvert, the name before 1.1).
#   ./packaging/uninstall.sh            the system-wide installation (asks for your password)
#   ./packaging/uninstall.sh --user     the per-user installation
#   --purge                             also delete your VertConvert&Create settings
# Your disk images (and the ones you converted or created) are never touched; qemu-img stays installed.
# (Arch package: sudo pacman -Rns vertconvert)
set -euo pipefail
APP_ID="io.github.oreo1298.VertConvertCreate"
OLD_ID="io.github.oreo1298.VirtConvert"
MODE=system
PURGE=0
for a in "$@"; do
  case "$a" in
    --user) MODE=user ;;
    --system) MODE=system ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $a (see --help)"; exit 2 ;;
  esac
done
installed() { [ -d "$1/vertconvert" ] || [ -d "$1/virtconvert" ]; }
if [ "$MODE" = system ] && ! installed /opt && installed "$HOME/.local/lib"; then
  MODE=user
fi

if [ "$MODE" = system ]; then
  if ! installed /opt; then
    echo "VertConvert&Create isn't installed system-wide (no /opt/vertconvert)."
    command -v pacman >/dev/null && pacman -Q vertconvert >/dev/null 2>&1 && \
      echo "It is installed as an Arch package: sudo pacman -Rns vertconvert"
    exit 0
  fi
  if [ "$(id -u)" != 0 ]; then exec sudo bash "$0" "$@"; fi
  LIB=/opt
  BIN=/usr/local/bin
  SHARE=/usr/local/share
else
  LIB="$HOME/.local/lib"
  BIN="$HOME/.local/bin"
  SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
fi
rm -rf "$LIB/vertconvert" "$LIB/virtconvert"
for id in "$APP_ID" "$OLD_ID"; do
  rm -f "$SHARE/applications/$id.desktop" "$SHARE/metainfo/$id.metainfo.xml" "$SHARE/icons/hicolor/scalable/apps/$id.svg"
done
rm -f "$BIN/vertconvert" "$BIN/virtconvert"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$SHARE/applications" 2>/dev/null || true
echo "VertConvert&Create was removed."
if [ "$PURGE" = 1 ]; then
  for home in "$HOME" ${SUDO_USER:+$(getent passwd "$SUDO_USER" | cut -d: -f6)}; do
    rm -rf "$home/.config/vertconvert" "$home/.cache/vertconvert" "$home/.config/virtconvert" "$home/.cache/virtconvert"
  done
  echo "Your VertConvert&Create settings were removed too."
else
  echo "Your settings (~/.config/vertconvert) were kept; --purge removes them."
fi
echo "Your disk images were not touched. qemu-img is still installed (remove it with your package manager if you like)."
