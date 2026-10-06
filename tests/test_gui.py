"""The app, offscreen: adding images, choosing targets, converting, cancelling."""

import os
import time

import pytest

pytest.importorskip("PySide6")
try:
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError as exc:  # Qt's libraries are missing (libEGL, ...)
    pytest.skip(f"Qt can't load: {exc}", allow_module_level=True)

from imagewriter import MIB, SECTOR, gpt_sectors, to_sectors, write_vdi, write_vhdx  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402

import vertconvert.gui.main_window as mw  # noqa: E402
from vertconvert.core import formats as F  # noqa: E402
from vertconvert.core.probe import probe  # noqa: E402
from vertconvert.gui.theme import theme  # noqa: E402

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
    from vertconvert.core.errors import OperationCancelled

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
