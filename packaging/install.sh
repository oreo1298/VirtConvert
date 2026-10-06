#!/usr/bin/env bash
# Install (or update) VertConvert&Create on any Linux distribution.
#
#   ./packaging/install.sh            system-wide into /opt/vertconvert, with the menu entry and
#                                     the `vertconvert` command in /usr/local/bin. Recommended.
#   ./packaging/install.sh --user     just for you, in ~/.local (no sudo for VertConvert&Create itself)
#
# Options:
#   --deps / --no-deps   install qemu-img, Python and Qt with your package manager (default: ask)
#   --yes                don't ask (installs the dependencies)
#   --python PATH        the Python 3.10+ to use (default: python3)
#
# Running it again updates the installation in place (your settings are kept).
# Arch-based distributions: the PKGBUILD (makepkg -si) is the better way.
set -euo pipefail

ORIG_ARGS=("$@")
MODE=system
DEPS=ask
YES=0
PY="${PYTHON:-python3}"
while [ $# -gt 0 ]; do
  case "$1" in
    --user) MODE=user ;;
    --system) MODE=system ;;
    --deps) DEPS=yes ;;
    --no-deps) DEPS=no ;;
    --yes|-y) YES=1; [ "$DEPS" = ask ] && DEPS=yes ;;
    --python) shift; PY="$1" ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ID="io.github.oreo1298.VertConvertCreate"

say() { printf '\033[1;34m>>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*"; exit 1; }
ask() {
  [ "$YES" = 1 ] && return 0
  [ -t 0 ] || return 1
  read -r -p "$1 [Y/n] " a
  case "${a:-y}" in y|Y|yes) return 0 ;; *) return 1 ;; esac
}

if [ "$MODE" = system ]; then
  if [ "$(id -u)" != 0 ]; then
    command -v sudo >/dev/null || die "Run this as root, or use --user"
    say "The system-wide install needs root; running it with sudo"
    exec sudo -E bash "$0" "${ORIG_ARGS[@]}"
  fi
  PREFIX=/opt/vertconvert
  BIN=/usr/local/bin
  SHARE=/usr/local/share
else
  [ "$(id -u)" = 0 ] && die "--user installs for a normal user; run it without sudo"
  PREFIX="$HOME/.local/lib/vertconvert"
  BIN="$HOME/.local/bin"
  SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
fi
VENV="$PREFIX/venv"
OLD_ID="io.github.oreo1298.VirtConvert"    # VirtConvert, the name before 1.1
# root's pip must not write into the caller's ~/.cache (sudo -E keeps HOME)
[ "$MODE" = system ] && export PIP_NO_CACHE_DIR=1
ROOT=""
[ "$(id -u)" != 0 ] && ROOT="sudo"

# ---------------------------------------------------------------------- dependencies
detect_pm() {
  if [ -e /run/ostree-booted ] && command -v rpm-ostree >/dev/null; then echo rpm-ostree; return; fi
  for pm in pacman apt-get dnf yum zypper xbps-install apk eopkg emerge nix-env; do
    command -v "$pm" >/dev/null && { echo "$pm"; return; }
  done
  echo none
}
PM="$(detect_pm)"

# qemu-img, Python with venv, PySide6 where the distribution packages it, and the libraries the
# PySide6 wheels from PyPI need where it doesn't (packages a release doesn't have are skipped).
pkgs_for() {
  case "$PM" in
    pacman) echo "python pyside6 qt6-svg qt6-wayland qemu-img" ;;
    apt-get) echo "python3 python3-venv qemu-utils python3-pyside6.qtwidgets python3-pyside6.qtsvg python3-pyside6.qtgui libegl1 libgl1 libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 libdbus-1-3 libglib2.0-0t64 libglib2.0-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-shape0" ;;
    dnf|yum) echo "python3 python3-pyside6 qemu-img mesa-libGL mesa-libEGL libxkbcommon libxkbcommon-x11 fontconfig dbus-libs xcb-util-cursor xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil" ;;
    zypper) echo "python3 python3-pyside6 qemu-tools Mesa-libGL1 Mesa-libEGL1 libxkbcommon0 libxkbcommon-x11-0 fontconfig libdbus-1-3 libglib-2_0-0 libgthread-2_0-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0" ;;
    xbps-install) echo "python3 python3-pyside6 qemu-img" ;;
    apk) echo "python3 py3-pip qemu-img py3-pyside6" ;;
    eopkg) echo "python3 qemu" ;;
    emerge) echo "app-emulation/qemu dev-python/pyside" ;;
    *) echo "" ;;
  esac
}

pm_install() {
  local pkgs="$*"
  case "$PM" in
    pacman) $ROOT pacman -S --needed --noconfirm $pkgs && return 0 ;;
    apt-get) DEBIAN_FRONTEND=noninteractive $ROOT apt-get install -y --no-install-recommends $pkgs && return 0 ;;
    dnf) $ROOT dnf install -y $pkgs && return 0 ;;
    yum) $ROOT yum install -y $pkgs && return 0 ;;
    zypper) $ROOT zypper --non-interactive install --no-recommends $pkgs && return 0 ;;
    xbps-install) $ROOT xbps-install -Sy $pkgs && return 0 ;;
    apk) $ROOT apk add $pkgs && return 0 ;;
    eopkg) $ROOT eopkg install -y $pkgs && return 0 ;;
    emerge) $ROOT emerge --noreplace $pkgs && return 0 ;;
  esac
  return 1
}

install_deps() {
  local pkgs; pkgs="$(pkgs_for)"
  case "$PM" in
    nix-env) warn "NixOS: add qemu-utils and python3Packages.pyside6 to your configuration"; return ;;
    rpm-ostree) warn "Immutable system: run 'sudo rpm-ostree install qemu-img', restart, then install with --user"; return ;;
    none) warn "Unknown package manager: install qemu-img (often in qemu-utils or qemu-tools) yourself"; return ;;
  esac
  if [ "$PM" = apt-get ]; then
    $ROOT apt-get update -qq || true
    local avail=""
    for p in $pkgs; do apt-cache show "$p" >/dev/null 2>&1 && avail="$avail $p"; done
    pkgs="$avail"
  fi
  say "Installing dependencies with $PM: $pkgs"
  if ! pm_install $pkgs; then
    warn "Some packages aren't available here; installing the others one by one"
    for p in $pkgs; do pm_install "$p" >/dev/null 2>&1 || warn "  not installed: $p"; done
  fi
}

qemu_img_found() {
  command -v qemu-img >/dev/null || [ -x /usr/bin/qemu-img ]
}

if [ "$DEPS" = yes ] || { [ "$DEPS" = ask ] && ! qemu_img_found && ask "Install qemu-img and the other dependencies with $PM?"; }; then
  install_deps || warn "Installing dependencies failed; continuing"
elif ! qemu_img_found; then
  warn "qemu-img is missing: VertConvert&Create needs it to write the converted disks. Run with --deps to install it."
fi

# ---------------------------------------------------------------------- Python 3.10+
# RHEL/Alma/Rocky 9 (3.9) and openSUSE Leap 15 (3.6) ship an older python3 next to newer ones.
py_ok() { command -v "$1" >/dev/null && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }
pick_python() {
  for c in "$PY" python3 python3.13 python3.12 python3.11 python3.10; do
    if py_ok "$c"; then PY="$c"; return 0; fi
  done
  return 1
}
if ! pick_python; then
  say "Python 3.10 or newer isn't installed; installing Python 3.11 with $PM"
  case "$PM" in
    dnf|yum) $ROOT "$PM" install -y python3.11 python3.11-pip || true ;;
    zypper) $ROOT zypper --non-interactive install python311 python311-pip || true ;;
    apt-get) $ROOT apt-get install -y python3.11 python3.11-venv || true ;;
    *) : ;;
  esac
  pick_python || die "Python 3.10 or newer is required (found: $(python3 --version 2>&1))"
fi
say "Using $("$PY" --version 2>&1) ($PY)"

# ---------------------------------------------------------------------- the app
say "Installing VertConvert&Create into $PREFIX"
mkdir -p "$PREFIX"
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv --system-site-packages "$VENV" || die "Could not create a virtualenv (Debian/Ubuntu: apt install python3-venv)"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
# build from a copy, so that (as root) nothing is written into your checkout
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
tar -C "$HERE" --exclude=./.git --exclude=./build --exclude=./dist --exclude='*.egg-info' \
    --exclude='__pycache__' --exclude=./.venv --exclude=./src --exclude=./pkg -cf - . | tar -C "$BUILD_DIR" -xf -
"$VENV/bin/python" -m pip install --quiet --upgrade --force-reinstall --no-deps "$BUILD_DIR" >/dev/null \
  || die "Installing VertConvert&Create into $VENV failed"
if "$VENV/bin/python" -c "import PySide6.QtWidgets, PySide6.QtSvg" 2>/dev/null; then
  say "PySide6 (Qt) is there"
else
  say "PySide6 isn't packaged here: installing PySide6-Essentials from PyPI (about 100 MB)"
  "$VENV/bin/python" -m pip install --quiet "PySide6-Essentials>=6.5" \
    || warn "Installing PySide6 failed: the command line works, the app doesn't (see the README)"
fi

# VertConvert&Create replaces VirtConvert (its name before 1.1)
OLD_PREFIX="$(dirname "$PREFIX")/virtconvert"
if [ -d "$OLD_PREFIX" ]; then
  say "Replacing VirtConvert (the old name of VertConvert&Create)"
  rm -rf "$OLD_PREFIX"
  rm -f "$BIN/virtconvert" "$SHARE/applications/$OLD_ID.desktop" "$SHARE/metainfo/$OLD_ID.metainfo.xml" \
        "$SHARE/icons/hicolor/scalable/apps/$OLD_ID.svg"
fi

mkdir -p "$BIN"
ln -sf "$VENV/bin/vertconvert" "$BIN/vertconvert"

say "Menu entry and icon"
install -Dm644 "$HERE/vertconvert/data/vertconvert.svg" "$SHARE/icons/hicolor/scalable/apps/$APP_ID.svg"
mkdir -p "$SHARE/applications"
sed "s#^Exec=vertconvert#Exec=$BIN/vertconvert#" "$HERE/data/$APP_ID.desktop" > "$SHARE/applications/$APP_ID.desktop"
install -Dm644 "$HERE/data/$APP_ID.metainfo.xml" "$SHARE/metainfo/$APP_ID.metainfo.xml"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$SHARE/applications" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$SHARE/icons/hicolor" 2>/dev/null || true

{
  echo "mode=$MODE"
  echo "source=$HERE"
  echo "version=$("$VENV/bin/python" -c 'import vertconvert; print(vertconvert.__version__)')"
} > "$PREFIX/.install-info"

if [ "$MODE" = user ]; then
  case ":$PATH:" in
    *":$BIN:"*) : ;;
    *) warn "Add $BIN to your PATH:  echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.profile" ;;
  esac
fi

echo
say "Done. Start VertConvert&Create from your application menu, or run: vertconvert"
"$VENV/bin/vertconvert" doctor || true
exit 0
