#!/usr/bin/env bash
# Install VertConvert&Create with packaging/install.sh inside a distribution container, use it, remove it.
# Used by CI:  docker run --rm -v "$PWD:/src:ro" IMAGE bash /src/tools/installer-smoke.sh
set -euo pipefail

if command -v apk >/dev/null; then apk add --no-cache bash >/dev/null; fi
if command -v xbps-install >/dev/null; then xbps-install -Syu xbps >/dev/null && xbps-install -Sy bash tar >/dev/null; fi
if command -v zypper >/dev/null; then zypper --non-interactive install --no-recommends tar gzip >/dev/null; fi
if command -v dnf >/dev/null; then dnf install -y tar >/dev/null; fi
# RHEL family: EPEL, as the README says
if grep -qiE '^ID="?(rocky|almalinux|rhel|centos)' /etc/os-release 2>/dev/null; then dnf install -y epel-release >/dev/null; fi

cp -r /src /tmp/vertconvert-src
cd /tmp/vertconvert-src
rm -rf .git

echo "== install"
bash ./packaging/install.sh --yes
command -v vertconvert
vertconvert --version
test -f /usr/local/share/applications/io.github.oreo1298.VertConvertCreate.desktop
test -f /usr/local/share/icons/hicolor/scalable/apps/io.github.oreo1298.VertConvertCreate.svg

echo "== convert both ways"
cd /tmp
/opt/vertconvert/venv/bin/python - <<'PY'
import os
with open("disk.raw", "wb") as f:
    f.truncate(8 << 20)
    f.write(os.urandom(1 << 20))
PY
qemu-img convert -f raw -O vhdx disk.raw disk.vhdx
vertconvert info disk.vhdx
vertconvert convert -q disk.vhdx              # -> disk.vdi (VirtualBox)
vertconvert convert -q -o back.vhdx disk.vdi  # -> back to Hyper-V
vertconvert verify -q disk.vhdx back.vhdx
vertconvert convert -q -t azure -o azure.vhd disk.vdi

echo "== new disks"
vertconvert create -q /tmp/empty.vhdx -s 100G
vertconvert create -q /tmp/data.vdi -s 1G --fs ext4 -L Data
vertconvert info /tmp/data.vdi | grep -q "GPT, 1 partition"
for fs in fat32 ntfs exfat; do
  vertconvert create -q "/tmp/$fs.vmdk" -s 256M --fs "$fs" || echo "(no $fs here)"
done

echo "== the app loads"
if [ "${REQUIRE_GUI:-1}" = 1 ]; then
  QT_QPA_PLATFORM=offscreen /opt/vertconvert/venv/bin/python -c \
    "from PySide6.QtWidgets import QApplication; app = QApplication([]); import vertconvert.gui.main_window as m; w = m.MainWindow(None); print('GUI OK')"
fi

echo "== update and uninstall"
cd /tmp/vertconvert-src
bash ./packaging/update.sh
bash ./packaging/uninstall.sh --purge
test ! -e /usr/local/bin/vertconvert
test ! -e /opt/vertconvert
echo "== all good"
