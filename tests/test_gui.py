"""The app, offscreen: adding images, choosing targets, converting, cancelling."""

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
try:
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError as exc:  # Qt's libraries are missing (libEGL, ...)
    pytest.skip(f"Qt can't load: {exc}", allow_module_level=True)

from imagewriter import MIB, SECTOR, gpt_sectors, mbr_sector, to_sectors, write_vdi, write_vhdx  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402

import virtconvert.gui.main_window as mw  # noqa: E402
from virtconvert.core import formats as F  # noqa: E402
from virtconvert.core.probe import probe  # noqa: E402
from virtconvert.gui.theme import theme  # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    theme.apply(application, "dark")
    return application


@pytest.fixture(autouse=True)
def no_modal(monkeypatch):
    monkeypatch.setattr(mw, "ask_yes_no", lambda *a, **k: True)
    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))


def pump(seconds=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def wait_for(condition, timeout=60):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        QCoreApplication.processEvents()
        time.sleep(0.005)
    pump(0.05)


@pytest.fixture
def window(app, qemu):
    win = mw.MainWindow(qemu)
    win.settings.clear()
    win.show()
    yield win
    win.runner.cancel()
    win.runner.wait(10000)
    win.close()


@pytest.fixture
def images(tmp_path):
    size = 8 * MIB
    gpt = gpt_sectors(size, [(2048, 8000, "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "EFI")])
    b = write_vhdx(tmp_path / "Win.vhdx", size, to_sectors(gpt))
    write_vhdx(tmp_path / "Win_1.avhdx", size, {9000: b"c" * SECTOR}, "differencing",
               parent=tmp_path / "Win.vhdx", parent_data_guid=b)
    write_vdi(tmp_path / "Linux.vdi", 6 * MIB, {0: os.urandom(MIB)})
    return tmp_path


def add(win, *paths):
    win.add_files([str(p) for p in paths])
    wait_for(lambda: all(i.state != mw.INSPECTING for i in win.model.items))


def test_empty_state(window):
    assert window.queue_stack.currentIndex() == 0
    assert not window.act_convert.isEnabled()
    assert window.img_empty.isVisible()


def test_add_and_defaults(window, images):
    add(window, images / "Win.vhdx", images / "Linux.vdi", images / "Win_1.avhdx")
    items = window.model.items
    assert [i.state for i in items] == [mw.READY] * 3
    assert items[0].target.format is F.VDI          # Hyper-V -> VirtualBox
    assert items[1].target.format is F.VHDX         # VirtualBox -> Hyper-V
    # the checkpoint is named after its disk, without clashing with Win.vhdx's output
    assert items[0].output.name == "Win.vdi" and items[2].output.name == "Win-2.vdi"
    assert window.queue_stack.currentIndex() == 1
    window.table.selectRow(2)
    pump()
    assert window.img_grid._values["chain"].text() == "Win.vhdx"
    assert window.img_grid._values["boot"].text() == "UEFI"
    assert "Enable EFI" in window.plan_banner.text()
    assert window.act_convert.isEnabled()


def test_duplicates_are_ignored(window, images):
    add(window, images / "Linux.vdi")
    add(window, images / "Linux.vdi")
    assert len(window.model.items) == 1


def test_change_target(window, images):
    add(window, images / "Win.vhdx")
    window.table.selectRow(0)
    pump()
    window.fmt_seg.set_value("vmdk")
    window._target_edited()
    item = window.model.items[0]
    assert item.target.format is F.VMDK and item.output.suffix == ".vmdk"
    assert window.vmdk_seg.isVisible() and not window.alloc_seg.isVisible()
    window.fmt_seg.set_value("vhd")
    window._target_edited()
    window.azure_chk.setChecked(True)
    assert item.target.azure and item.target.fixed
    # remembered for the next Hyper-V image
    assert window.settings.value("target_vbox") == "vhd:azure"


def test_convert_all(window, images):
    add(window, images / "Win_1.avhdx", images / "Linux.vdi")
    window.convert_all()
    wait_for(lambda: all(i.state in (mw.DONE, mw.FAILED) for i in window.model.items) and not window.runner.busy)
    items = window.model.items
    assert [i.state for i in items] == [mw.DONE, mw.DONE], [i.message for i in items]
    assert all(i.outcome.verified for i in items)
    assert probe(images / "Win.vdi").format is F.VDI
    assert probe(images / "Linux.vhdx").format is F.VHDX
    assert window.act_clear.isEnabled()
    window.clear_finished()
    assert not window.model.items


def test_cancel(window, images, monkeypatch):
    from virtconvert.core.errors import OperationCancelled

    def slow_execute(plan, qemu, progress, cancel, log=None):
        progress("Converting", 1, 100)
        while not cancel():
            time.sleep(0.01)
        raise OperationCancelled()

    monkeypatch.setattr(mw, "execute", slow_execute)
    add(window, images / "Linux.vdi", images / "Win.vhdx")
    window.convert_all()
    wait_for(lambda: window.model.items[0].state == mw.RUNNING and window.model.items[0].done == 1)
    assert window.model.items[1].state == mw.WAITING
    assert window.cancel_btn.isVisible() and window.act_cancel.isEnabled()
    window.cancel_operation()
    wait_for(lambda: not window.runner.busy)
    pump(0.1)
    # the running one is cancelled, the waiting one goes back to ready
    assert [i.state for i in window.model.items] == [mw.CANCELLED, mw.READY]
    assert not window.cancel_btn.isVisible()


def test_blocked_item(window, tmp_path):
    (tmp_path / "junk.vhd").write_bytes(b"not an image" * 100)
    add(window, tmp_path / "junk.vhd")
    item = window.model.items[0]
    assert item.state == mw.BLOCKED
    assert not window.act_convert.isEnabled()


def test_missing_parent_offers_choose(window, tmp_path):
    import uuid
    write_vhdx(tmp_path / "orphan.avhdx", 4 * MIB, {}, "differencing", parent=tmp_path / "gone.vhdx",
               parent_data_guid=uuid.uuid4())
    add(window, tmp_path / "orphan.avhdx")
    window.table.selectRow(0)
    pump()
    assert window.model.items[0].state == mw.BLOCKED
    assert window.parent_btn.isVisible()
    assert window.act_parent.isEnabled()


def test_theme_toggle(window, images):
    add(window, images / "Linux.vdi")
    window.set_theme("light")
    assert not theme.palette.dark
    window.set_theme("dark")
    assert theme.palette.dark


def test_without_qemu(app):
    win = mw.MainWindow(None)
    win.show()
    pump()
    assert win.qemu_text.text() == "qemu-img missing"
    assert win.act_install_qemu.isVisible()
    win.close()


def test_typed_name_is_used(window, images):
    add(window, images / "Linux.vdi")
    window.table.selectRow(0)
    pump()
    window.out_edit.setText(str(images / "typed.vhdx"))   # no Enter, straight to Convert
    window.convert_all()
    wait_for(lambda: window.model.items[0].state in (mw.DONE, mw.FAILED) and not window.runner.busy)
    assert window.model.items[0].state == mw.DONE
    assert probe(images / "typed.vhdx").format is F.VHDX
    assert not (images / "Linux.vhdx").exists()


def test_replacing_one_file_is_not_a_global_setting(window, images, monkeypatch):
    (images / "existing.vhdx").write_bytes(b"old")
    add(window, images / "Linux.vdi")
    window.table.selectRow(0)
    pump()
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(images / "existing.vhdx"), "")))
    window.choose_output()
    item = window.model.items[0]
    assert item.overwrite and not window.overwrite_chk.isChecked()
    assert item.state == mw.READY and any("will be replaced" in w for w in item.plan.warnings)


def make_appliance(folder, qemu, disks):
    """An OVA with DISKS (name -> raw bytes) as stream-optimized VMDKs."""
    import io
    import tarfile
    members = {}
    for name, data in disks.items():
        raw = folder / f"{name}.raw"
        raw.write_bytes(data)
        qemu.check_run(["convert", "-f", "raw", "-O", "vmdk", "-o", "subformat=streamOptimized", str(raw),
                        str(folder / name)], "test")
        members[name] = (folder / name).read_bytes()
        raw.unlink()
        (folder / name).unlink()
    refs = "".join(f'<File ovf:href="{n}" ovf:id="f{i}"/>' for i, n in enumerate(members))
    items = "".join(f'<Disk ovf:capacity="{len(d)}" ovf:diskId="d{i}" ovf:fileRef="f{i}"/>'
                    for i, d in enumerate(disks.values()))
    ovf = (f'<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/'
           f'envelope/1"><References>{refs}</References><DiskSection>{items}</DiskSection></Envelope>').encode()
    path = folder / "App.ova"
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in [("App.ovf", ovf), *members.items()]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_appliance_disks_are_items(window, qemu, tmp_path):
    disks = {"system.vmdk": os.urandom(MIB) + bytes(MIB), "data.vmdk": bytes(MIB) + os.urandom(MIB)}
    path = make_appliance(tmp_path, qemu, disks)
    add(window, path)
    add(window, path)                                        # again: nothing new
    items = window.model.items
    assert [i.name for i in items] == ["App.ova › system.vmdk", "App.ova › data.vmdk"]
    assert [i.from_label for i in items] == ["VMDK in OVA"] * 2
    assert [i.output.name for i in items] == ["App-system.vhdx", "App-data.vhdx"]   # VMware/VirtualBox -> Hyper-V
    window.convert_all()
    wait_for(lambda: all(i.state in (mw.DONE, mw.FAILED) for i in items) and not window.runner.busy)
    assert [i.state for i in items] == [mw.DONE, mw.DONE], [i.message for i in items]
    for item, data in zip(items, disks.values()):
        out = tmp_path / "check.raw"
        qemu.check_run(["convert", "-O", "raw", str(item.output), str(out)], "test")
        assert out.read_bytes() == data


def test_vmdk_variants_and_memory(window, images, qemu):
    add(window, images / "Win.vhdx")
    window.table.selectRow(0)
    pump()
    window.fmt_seg.set_value("vmdk")
    window._target_edited()
    window.vmdk_seg.set_value("fixed")
    window.split_chk.setChecked(True)                   # emits: the picker reports the change
    item = window.model.items[0]
    assert item.target.label == "VMDK (preallocated, 2 GB files)"
    assert window.settings.value("target_vbox") == "vmdk:fixed+split"
    window.vmdk_seg.set_value("stream")
    window.picker._edited()
    assert item.target.stream and not item.target.split and not window.split_chk.isEnabled()
    # a QCOW2 disk goes to VirtualBox too, and gets the remembered choice; a VMDK goes to Hyper-V
    qcow = images / "kvm.qcow2"
    qemu.check_run(["create", "-q", "-f", "qcow2", str(qcow), "4M"], "test")
    qemu.check_run(["create", "-q", "-f", "vmdk", str(images / "ws.vmdk"), "4M"], "test")
    add(window, qcow, images / "ws.vmdk")
    assert window.model.items[1].target.label == "VMDK (stream-optimized)"
    assert window.model.items[2].target.format is F.VHDX


def test_new_disk(window, tmp_path, monkeypatch):
    from virtconvert.gui import create_dialog

    def fill_in(dialog):
        dialog.picker.fmt_seg.set_value("vhdx")
        dialog.picker._edited()
        dialog._format_changed()
        assert dialog.out_edit.text().endswith(".vhdx")
        dialog.size_spin.setValue(300)
        dialog.unit_box.setCurrentIndex(0)              # MB
        dialog.contents_seg.set_value("formatted")
        dialog._contents_changed()
        dialog.fs_box.setCurrentIndex(dialog.fs_box.findData("exfat" if mw.system.find_program("mkfs.exfat")
                                                              else "ext4"))
        dialog.label_edit.setText("Shared")
        dialog.out_edit.setText(str(tmp_path / "Shared.vhdx"))
        dialog._replan()
        assert dialog.create_btn.isEnabled(), dialog.plan.errors
        dialog._create()
        return dialog.result()

    monkeypatch.setattr(create_dialog.CreateDiskDialog, "exec", fill_in)
    window.new_disk()
    wait_for(lambda: not window.runner.busy)
    info = probe(tmp_path / "Shared.vhdx")
    assert info.ok and info.format is F.VHDX and info.virtual_size == 300 * MIB
    assert window.settings.value("create/target") == "vhdx"
    assert window.settings.value("create/contents") == "formatted"
    # the dialog starts from the last choices, next to the last disk, with a free name
    dialog = create_dialog.CreateDiskDialog(window, window.qemu, window.settings)
    assert dialog.picker.target().format is F.VHDX and dialog.contents_seg.value() == "formatted"
    assert Path(dialog.out_edit.text()) == tmp_path / "New disk.vhdx"
    dialog._replan()
    assert dialog.plan.ok
    dialog.out_edit.setText(str(tmp_path / "Shared.vhdx"))
    dialog._replan()
    assert not dialog.create_btn.isEnabled() and "already exists" in dialog.banner.text()


@pytest.fixture
def loop_device(tmp_path):
    import shutil
    import subprocess
    if os.geteuid() != 0 or not shutil.which("losetup"):
        pytest.skip("needs root and losetup for a loop device")
    data = bytearray(16 * MIB)
    data[0:SECTOR] = mbr_sector([(2048, 30000, 0x83, True)])[0]
    data[3 * MIB:4 * MIB] = os.urandom(MIB)
    (tmp_path / "disk.img").write_bytes(bytes(data))
    res = subprocess.run(["losetup", "-f", "--show", str(tmp_path / "disk.img")], capture_output=True, text=True)
    if res.returncode != 0:
        pytest.skip(f"no loop device: {res.stderr.strip()}")
    yield res.stdout.strip(), bytes(data)
    subprocess.run(["losetup", "-d", res.stdout.strip()])


def test_copy_a_physical_disk(window, qemu, tmp_path, monkeypatch, loop_device):
    from virtconvert.core import devices
    from virtconvert.gui import create_dialog

    device, data = loop_device
    real_list = devices.list_disks
    monkeypatch.setattr(devices, "list_disks", lambda include_loop=False: real_list(include_loop=True))

    def read_back(path, driver):
        out = tmp_path / "back.raw"
        qemu.check_run(["convert", "-f", driver, "-O", "raw", str(path), str(out)], "test")
        return out.read_bytes()

    # New disk → Copy of a physical disk
    def fill_in(dialog):
        dialog.source_seg.set_value("physical")
        dialog._source_changed()
        index = dialog.chooser.box.findData(device)
        assert index >= 0, [dialog.chooser.box.itemData(i) for i in range(dialog.chooser.box.count())]
        dialog.chooser.box.setCurrentIndex(index)
        assert Path(dialog.out_edit.text()).name.startswith(Path(device).name)     # named after the disk
        assert not dialog.size_box.isVisibleTo(dialog) and dialog.verify_chk.isVisibleTo(dialog)
        dialog.out_edit.setText(str(tmp_path / "copy.vdi"))
        dialog._name_typed("")
        dialog._replan()
        assert dialog.create_btn.isEnabled() and dialog.create_btn.text() == "Copy the disk", dialog.plan.errors
        assert "a copy of" in dialog.banner.text()
        dialog._create()
        return dialog.result()

    monkeypatch.setattr(create_dialog.CreateDiskDialog, "exec", fill_in)
    window.new_disk()
    wait_for(lambda: not window.runner.busy)
    assert read_back(tmp_path / "copy.vdi", "vdi") == data
    assert window.settings.value("create/source") == "physical"

    # Add physical disk… → the list, converted like an image
    monkeypatch.setattr(create_dialog.DiskPickerDialog, "exec",
                        lambda dialog: dialog.chooser.box.setCurrentIndex(dialog.chooser.box.findData(device)) or 1)
    window.add_physical_disk()
    wait_for(lambda: window.model.items and window.model.items[0].state != mw.INSPECTING)
    item = window.model.items[0]
    assert item.info.device and item.from_label.startswith("Physical disk") and item.state == mw.READY
    assert item.output.parent != Path("/dev")
    window.table.selectRow(0)
    pump()
    window.out_edit.setText(str(tmp_path / "listed.vdi"))      # as typed into Save as
    window.convert_all()
    wait_for(lambda: item.state in (mw.DONE, mw.FAILED) and not window.runner.busy)
    assert item.state == mw.DONE, item.message
    assert item.output == tmp_path / "listed.vdi"                 # a physical disk goes to VirtualBox
    assert read_back(item.output, "vdi") == data


def test_progress_beyond_2_gb(window):
    """Byte counts over 2 GB reach the progress bar intact (a C++ int would wrap)."""
    total = 5 << 30

    def job(progress, cancel):
        progress("Converting", 3 << 30, total)
        time.sleep(0.2)
        return None

    seen = []
    window.runner.progress.connect(lambda stage, done, t: seen.append((done, t)))
    window._start("Big", job, lambda _r: None)
    wait_for(lambda: not window.runner.busy)
    assert (3 << 30, total) in seen


def test_target_edits_change_only_what_was_changed(window, images, qemu):
    add(window, images / "Win.vhdx", images / "Linux.vdi")
    vhdx, vdi = window.model.items
    # one image becomes a dynamic VHD, the other a stream-optimized VMDK
    window.table.selectRow(0)
    pump()
    window.fmt_seg.set_value("vhd")
    window.picker._edited()
    window.table.selectRow(1)
    pump()
    window.fmt_seg.set_value("vmdk")
    window.picker._edited()
    window.vmdk_seg.set_value("stream")
    window.picker._edited()
    assert vhdx.target.label == "VHD (dynamic)" and vdi.target.label == "VMDK (stream-optimized)"
    # both selected, Fixed clicked: the VHD becomes fixed, the VMDK keeps its format and variant
    window.table.selectAll()
    pump()
    assert window.current_item() is vhdx
    window.alloc_seg.set_value("fixed")
    window.picker._edited()
    assert vhdx.target.label == "VHD (fixed)" and vdi.target.label == "VMDK (stream-optimized)"
    # clicking the chosen format again doesn't touch a finished image
    window.table.selectRow(0)
    pump()
    vhdx.state = mw.DONE
    output = vhdx.output
    window.fmt_seg.set_value("vhd")
    window.picker._edited()
    assert vhdx.state == mw.DONE and vhdx.output == output


def test_names_too_long_block_the_item(window, tmp_path, monkeypatch):
    import errno

    def refuse(*a, **k):
        raise OSError(errno.ENAMETOOLONG, "File name too long", str(tmp_path / ("x" * 300)))

    monkeypatch.setattr(mw, "default_output", refuse)
    write_vdi(tmp_path / "a.vdi", 2 * MIB, {})
    add(window, tmp_path / "a.vdi")
    item = window.model.items[0]
    assert item.state == mw.BLOCKED and item.message.startswith("File name too long"), item.message


def test_save_dialog_consent_is_for_its_own_file(window, images, monkeypatch):
    (images / "taken.vhdx").write_bytes(b"old")
    add(window, images / "Linux.vdi")
    window.table.selectRow(0)
    pump()
    # the dialog returned "taken" (which doesn't exist); "taken.vhdx" does, and was never asked about
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(images / "taken"), "")))
    window.choose_output()
    item = window.model.items[0]
    assert item.output == images / "taken.vhdx" and not item.overwrite and item.state == mw.BLOCKED
