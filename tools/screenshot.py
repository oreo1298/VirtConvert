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
from virtconvert.core.qemu import QemuImg  # noqa: E402
from virtconvert.gui.app import app_icon  # noqa: E402
from virtconvert.gui.theme import theme  # noqa: E402

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
    return [router, win, child, ubuntu, xp]


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
        win.close()
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
