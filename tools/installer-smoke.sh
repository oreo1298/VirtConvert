#!/usr/bin/env bash
# Install VirtConvert&Create with packaging/install.sh inside a distribution container, use it, remove it.
# Used by CI:  docker run --rm -v "$PWD:/src:ro" IMAGE bash /src/tools/installer-smoke.sh
set -euo pipefail

if command -v apk >/dev/null; then apk add --no-cache bash >/dev/null; fi
if command -v xbps-install >/dev/null; then xbps-install -Syu xbps >/dev/null && xbps-install -Sy bash tar >/dev/null; fi
if command -v zypper >/dev/null; then zypper --non-interactive install --no-recommends tar gzip >/dev/null; fi
if command -v dnf >/dev/null; then dnf install -y tar >/dev/null; fi
# RHEL family: EPEL, as the README says
if grep -qiE '^ID="?(rocky|almalinux|rhel|centos)' /etc/os-release 2>/dev/null; then dnf install -y epel-release >/dev/null; fi

cp -r /src /tmp/virtconvert-src
cd /tmp/virtconvert-src
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

echo "== new disks"
virtconvert create -q /tmp/empty.vhdx -s 100G
virtconvert create -q /tmp/data.vdi -s 1G --fs ext4 -L Data
virtconvert info /tmp/data.vdi | grep -q "GPT, 1 partition"
for fs in fat32 ntfs exfat; do
  virtconvert create -q "/tmp/$fs.vmdk" -s 256M --fs "$fs" || echo "(no $fs here)"
done

echo "== writing onto a disk (refused for a folder: no disk to spare in a container)"
if virtconvert write -y disk.vhdx /tmp > write.out 2>&1; then cat write.out; exit 1; fi
grep -q "is not a disk" write.out

echo "== the app loads"
if [ "${REQUIRE_GUI:-1}" = 1 ]; then
  QT_QPA_PLATFORM=offscreen /opt/virtconvert/venv/bin/python -c \
    "from PySide6.QtWidgets import QApplication; app = QApplication([]); import virtconvert.gui.main_window as m; w = m.MainWindow(None); print('GUI OK')"
fi

echo "== update and uninstall"
cd /tmp/virtconvert-src
bash ./packaging/update.sh
bash ./packaging/uninstall.sh --purge
test ! -e /usr/local/bin/virtconvert
test ! -e /opt/virtconvert
echo "== all good"
