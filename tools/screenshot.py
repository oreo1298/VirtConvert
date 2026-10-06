#!/usr/bin/env python3
"""Render the app offscreen with demo images and save screenshots.

Usage: tools/screenshot.py OUTPUT_DIR [--theme dark|light] [--size 1440x900]

The demo images are written by tests/imagewriter.py into a temporary folder;
one small conversion really runs, the progress screenshot is staged.
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="virtconvert-shot-"))

import imagewriter as W  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import virtconvert.gui.main_window as mw  # noqa: E402
from virtconvert.core import devices, ova  # noqa: E402
from virtconvert.core.qemu import QemuImg  # noqa: E402
from virtconvert.gui.app import app_icon  # noqa: E402
from virtconvert.gui.create_dialog import CreateDiskDialog  # noqa: E402
from virtconvert.gui.theme import theme  # noqa: E402
from virtconvert.gui.write_dialog import WriteDiskDialog  # noqa: E402

GIB = 1 << 30
EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MSR = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def wait(win, condition, timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    pump(0.3)


def make_demo(folder: Path) -> list[Path]:
    hyperv = folder / "Hyper-V"
    vbox = folder / "VirtualBox VMs" / "Ubuntu Server"
    hyperv.mkdir(parents=True)
    vbox.mkdir(parents=True)

    size = 64 * GIB
    lbas = size // 512
    gpt = W.gpt_sectors(size, [(2048, 206847, EFI, "EFI system partition"),
                               (206848, 239615, MSR, "Microsoft reserved partition"),
                               (239616, lbas - 1026048 - 1, DATA, "Basic data partition"),
                               (lbas - 1026048, lbas - 34, RECOVERY, "")])
    base = W.to_sectors(gpt)
    base.update({s: os.urandom(512) for s in range(2048, 2048 + 4096)})
    win = hyperv / "Windows 11.vhdx"
    guid = W.write_vhdx(win, size, base, creator="Microsoft Windows (demo image)")
    child = hyperv / f"Windows 11_{str(uuid.uuid4()).upper()}.avhdx"
    W.write_vhdx(child, size, {s: os.urandom(512) for s in range(300000, 302048)}, "differencing",
                 parent=win, parent_data_guid=guid, creator="Microsoft Windows (demo image)")

    size = 25 * GIB
    ubuntu = vbox / "Ubuntu Server.vdi"
    mbr = W.mbr_sector([(2048, 2 * 1024 * 2048, 0x83, True), (4196352, 4 * 1024 * 2048, 0x82, False),
                        (12584960, size // 512 - 12584960, 0x83, False)])
    W.write_vdi(ubuntu, size, {0: mbr[0].ljust(1 << 20, b"\0")})

    xp = folder / "Old PCs" / "Windows XP.vhd"
    xp.parent.mkdir()
    W.write_vhd(xp, 16 * GIB, W.to_sectors(W.mbr_sector([(63, 16 * GIB // 512 - 63, 0x07, True)])),
                creator=b"vpc ")

    router = folder / "Hyper-V" / "pfSense.vhdx"
    W.write_vhdx(router, 1 * GIB, W.to_sectors(W.mbr_sector([(63, 2 * 1024 * 1024 - 63, 0xA5, True)])),
                 creator="Microsoft Windows (demo image)")

    # an appliance downloaded from somewhere: a stream-optimized VMDK inside an OVA
    appliances = folder / "Downloads"
    appliances.mkdir()
    raw = appliances / "disk.raw"
    with open(raw, "wb") as f:
        f.truncate(20 * GIB)
        f.write(W.mbr_sector([(2048, 20 * GIB // 512 - 2048, 0x83, True)])[0])
    vmdk = appliances / "Debian-disk1.vmdk"
    QemuImg.find().check_run(["convert", "-q", "-f", "raw", "-O", "vmdk", "-o", "subformat=streamOptimized",
                              str(raw), str(vmdk)], "demo")
    raw.unlink()
    appliance = appliances / "Debian 12 appliance.ova"
    ova.write_ova(appliance, vmdk, "Debian", 20 * GIB, os_hint="linux")
    vmdk.unlink()
    return [router, win, child, ubuntu, xp, appliance]


def demo_disks() -> list:
    """Two disks as lsblk would describe them: a second SSD with Windows on it, and a USB stick."""
    D, P = devices.PhysicalDisk, devices.DiskPart
    ssd = D(path="/dev/sdb", name="sdb", size=500107862016, model="Samsung SSD 870 EVO 500GB", transport="sata",
            table="gpt", partitions=[
                P("sdb1", "/dev/sdb1", 104857600, "vfat", "", EFI),
                P("sdb2", "/dev/sdb2", 16777216, "", "", MSR),
                P("sdb3", "/dev/sdb3", 499360923648, "ntfs", "Windows", DATA),
                P("sdb4", "/dev/sdb4", 625999872, "ntfs", "", RECOVERY)])
    stick = D(path="/dev/sdc", name="sdc", size=31004295168, model="Ultra", vendor="SanDisk", transport="usb",
              removable=True, table="dos", partitions=[P("sdc1", "/dev/sdc1", 31003246592, "exfat", "STICK", "0x7")],
              mountpoints=["/run/media/me/STICK"], mounted=[("/dev/sdc1", "/run/media/me/STICK")])
    return [ssd, stick]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--theme", default="dark")
    ap.add_argument("--size", default="1440x900")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    w, h = (int(v) for v in args.size.split("x"))

    mw.ask_yes_no = lambda *a, **k: True
    from PySide6.QtWidgets import QMessageBox
    for name in ("information", "warning", "critical", "question"):
        setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))

    tmp = Path(tempfile.mkdtemp(prefix="virtconvert-demo-"))
    try:
        app = QApplication([])
        app.setWindowIcon(app_icon())
        theme.apply(app, args.theme)
        files = make_demo(tmp)
        win = mw.MainWindow(QemuImg.find())
        win.resize(w, h)
        win.show()
        pump(0.4)
        win.grab().save(str(args.out / f"empty-{args.theme}.png"))

        win.add_files([str(f) for f in files])
        wait(win, lambda: all(i.state != mw.INSPECTING for i in win.model.items))
        # the small router disk really gets converted
        router = win.model.items[0]
        for item in win.model.items[1:]:
            item.state = mw.BLOCKED  # keep them out of this run
        win.convert_all()
        wait(win, lambda: router.state in (mw.DONE, mw.FAILED) and not win.runner.busy, 120)
        for item in win.model.items[1:]:
            item.state = mw.READY
            win._replan(item)
        win.toast.hide()
        win.table.selectRow(2)
        pump(0.4)
        win.grab().save(str(args.out / f"main-{args.theme}.png"))

        # going back to Hyper-V
        win.table.selectRow(3)
        pump(0.4)
        win.grab().save(str(args.out / f"back-{args.theme}.png"))

        # a conversion in progress (staged: the progress values are set by hand)
        item = win.model.items[1]
        win.table.selectRow(1)
        item.state = mw.RUNNING
        win._running = item
        win._set_op_state(True, "Converting…")
        win._stage = "Converting"
        win._stage_t0 = time.monotonic() - 74
        win._on_progress("Converting", int(item.info.virtual_size * 0.43), item.info.virtual_size)
        win.cancel_btn.show()
        win.progress.show()
        win._selection_changed()
        pump(0.3)
        win.grab().save(str(args.out / f"progress-{args.theme}.png"))
        win._running = None
        item.state = mw.READY
        win._set_op_state(False, "Ready")

        # the appliance, going to QEMU/KVM
        win.table.selectRow(5)
        pump(0.2)
        win.fmt_seg.set_value("qcow2")
        win._target_edited()
        pump(0.4)
        win.grab().save(str(args.out / f"appliance-{args.theme}.png"))

        # New disk
        win.settings.setValue("create/last_output", str(tmp / "Hyper-V" / "x.vhdx"))
        dialog = CreateDiskDialog(win, win.qemu, win.settings)
        dialog.picker.fmt_seg.set_value("vhdx")
        dialog.picker._edited()
        dialog._format_changed()
        dialog.size_spin.setValue(256)
        dialog.contents_seg.set_value("formatted")
        dialog._contents_changed()
        dialog.fs_box.setCurrentIndex(dialog.fs_box.findData("ntfs"))
        dialog.label_edit.setText("Data")
        dialog.out_edit.setText(str(tmp / "Hyper-V" / "Data.vhdx"))
        dialog._replan()
        dialog.show()
        pump(0.5)
        dialog.grab().save(str(args.out / f"create-{args.theme}.png"))
        dialog.close()

        # New disk from a physical disk (demo disks: this computer's own are not shown)
        demo = demo_disks()
        devices.list_disks = lambda include_loop=False: demo
        devices.inspect_device = lambda path: devices.info_from_disk(next(d for d in demo if d.path == str(path)))
        win.settings.setValue("create/source", "physical")
        dialog = CreateDiskDialog(win, win.qemu, win.settings)
        dialog.picker.fmt_seg.set_value("vdi")
        dialog.picker._edited()
        dialog._format_changed()
        dialog._replan()
        dialog.show()
        pump(0.5)
        dialog.grab().save(str(args.out / f"physical-{args.theme}.png"))
        dialog.close()

        # Write to disk: the Ubuntu disk onto the USB stick (demo disks again)
        devices.is_device = lambda path: str(path).startswith("/dev/")
        devices.disk_info = lambda path: next(d for d in demo if d.path == str(path))
        devices.base_disks = lambda path: {Path(path).name}
        win.table.selectRow(3)
        pump(0.2)
        item = win.current_item()
        dialog = WriteDiskDialog(win, win.qemu, win.settings, item.source, item.info)
        dialog.show()
        pump(0.5)
        dialog.grab().save(str(args.out / f"write-{args.theme}.png"))
        dialog.close()
        win.close()
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
