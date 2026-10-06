#!/usr/bin/env bash
# Install VirtConvert with packaging/install.sh inside a distribution container, use it, remove it.
# Used by CI:  docker run --rm -v "$PWD:/src:ro" IMAGE bash /src/tools/installer-smoke.sh
set -euo pipefail

if command -v apk >/dev/null; then apk add --no-cache bash >/dev/null; fi
if command -v xbps-install >/dev/null; then xbps-install -Syu xbps >/dev/null && xbps-install -Sy bash tar >/dev/null; fi
if command -v zypper >/dev/null; then zypper --non-interactive install --no-recommends tar gzip >/dev/null; fi
if command -v dnf >/dev/null; then dnf install -y tar >/dev/null; fi

cp -r /src /tmp/VirtConvert
cd /tmp/VirtConvert
rm -rf .git

echo "== install"
bash ./packaging/install.sh --yes
command -v virtconvert
virtconvert --version
test -f /usr/local/share/applications/io.github.oreo1298.VirtConvert.desktop
test -f /usr/local/share/icons/hicolor/scalable/apps/io.github.oreo1298.VirtConvert.svg

echo "== convert both ways"
cd /tmp
/opt/virtconvert/venv/bin/python - <<'PY'
import os
with open("disk.raw", "wb") as f:
    f.truncate(8 << 20)
    f.write(os.urandom(1 << 20))
PY
qemu-img convert -f raw -O vhdx disk.raw disk.vhdx
virtconvert info disk.vhdx
virtconvert convert -q disk.vhdx              # -> disk.vdi (VirtualBox)
virtconvert convert -q -o back.vhdx disk.vdi  # -> back to Hyper-V
virtconvert verify -q disk.vhdx back.vhdx
virtconvert convert -q -t azure -o azure.vhd disk.vdi

echo "== the app loads"
if [ "${REQUIRE_GUI:-1}" = 1 ]; then
  QT_QPA_PLATFORM=offscreen /opt/virtconvert/venv/bin/python -c \
    "from PySide6.QtWidgets import QApplication; app = QApplication([]); import virtconvert.gui.main_window as m; w = m.MainWindow(None); print('GUI OK')"
fi

echo "== update and uninstall"
cd /tmp/VirtConvert
bash ./packaging/update.sh
bash ./packaging/uninstall.sh --purge
test ! -e /usr/local/bin/virtconvert
test ! -e /opt/virtconvert
echo "== all good"
