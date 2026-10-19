"""The New disk dialog: an empty or formatted disk, or a copy of a physical disk."""

from __future__ import annotations

import html
import os
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import devices
from ..core import formats as F
from ..core.convert import Job, Plan, make_plan
from ..core.create import FILESYSTEMS, CreatePlan, NewDisk, available_filesystems, make_create_plan
from ..core.errors import ConvertError
from ..core.probe import ImageInfo
from ..core.qemu import QemuImg
from ..core.util import GIB, MIB, TIB, human_bytes
from . import icons
from .pickers import TargetPicker, target_from_setting
from .theme import theme
from .widgets import SegmentedControl, scaled_font

UNITS = (("MB", MIB), ("GB", GIB), ("TB", TIB))
CONTENTS = (
    ("empty", "Empty", "Nothing on it yet: install an operating system, or partition it in the VM"),
    ("partitioned", "Partitioned", "A partition table with one partition, left unformatted"),
    ("formatted", "Formatted", "One partition with a file system, ready to use as an extra disk"),
)
SOURCES = (
    ("new", "New disk", "An empty disk, or one with a formatted partition"),
    ("physical", "Copy of a physical disk", "A virtual disk with everything on one of this computer's disks: "
                                            "a hard disk, an SSD, a USB stick or a memory card"),
)


class DiskChooser(QWidget):
    """The computer's physical disks in a list, with a refresh button and a description. For
    writing onto a disk (FOR_WRITING), the disk the system runs from is marked, and a removable
    one is chosen first. With PARTITIONS, the whole disk or some of its partitions can be chosen
    (selection())."""

    changed = Signal()

    def __init__(self, parent=None, for_writing: bool = False, partitions: bool = False):
        super().__init__(parent)
        self.for_writing = for_writing
        self.with_partitions = partitions
        self._checks: list[tuple[QCheckBox, devices.DiskPart]] = []
        self._checks_of: str | None = None      # the disk the ticks are for
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.box = QComboBox()
        self.box.currentIndexChanged.connect(lambda _i: self._selected())
        self.refresh_btn = QToolButton()
        self.refresh_btn.setObjectName("Flat")
        self.refresh_btn.setToolTip("Look for disks again (after plugging one in)")
        self.refresh_btn.setIcon(icons.icon("refresh", theme.palette.text_muted))
        self.refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self.box, 1)
        row.addWidget(self.refresh_btn)
        lay.addLayout(row)
        self.desc = QLabel()
        self.desc.setObjectName("Muted")
        self.desc.setWordWrap(True)
        lay.addWidget(self.desc)
        if partitions:
            self.scope_seg = SegmentedControl()
            self.scope_seg.add("Whole disk", "whole", "Everything on the disk, its partition table included")
            self.scope_seg.add("Some partitions", "parts", "Only the partitions you tick, in a new disk just their "
                                                           "size (only they are read)")
            self.scope_seg.set_value("whole")
            self.scope_seg.changed.connect(lambda _v: self._scope_changed())
            lay.addWidget(self.scope_seg)
            self.parts_box = QWidget()
            self.parts_lay = QVBoxLayout(self.parts_box)
            self.parts_lay.setContentsMargins(4, 2, 0, 0)
            self.parts_lay.setSpacing(4)
            self.parts_box.hide()
            lay.addWidget(self.parts_box)
        self.disks: list[devices.PhysicalDisk] = []
        self.error = ""

    def refresh(self) -> None:
        current = self.path()
        try:
            self.disks = devices.list_disks()
            self.error = ""
        except ConvertError as exc:
            self.disks, self.error = [], str(exc)
        self.box.blockSignals(True)
        self.box.clear()
        for d in self.disks:
            mark = "  ·  the running system" if self.for_writing and d.system else ""
            self.box.addItem(f"{d.title}  ·  {human_bytes(d.size)}{mark}", d.path)
            self.box.setItemData(self.box.count() - 1, f"{d.path}\n{d.summary}", Qt.ToolTipRole)
        if not self.disks:
            self.box.addItem(self.error or "No disks found", None)
        index = self.box.findData(current) if current else -1
        if index < 0 and self.for_writing:
            # not the system's disk: a USB stick or memory card, else any other disk
            ranked = sorted(range(len(self.disks)), key=lambda i: (self.disks[i].system, not self.disks[i].removable))
            index = ranked[0] if ranked and not self.disks[ranked[0]].system else -1
        self.box.setCurrentIndex(max(0, index))
        self.box.blockSignals(False)
        self._selected()

    def disk(self) -> devices.PhysicalDisk | None:
        path = self.box.currentData()
        return next((d for d in self.disks if d.path == path), None)

    def path(self) -> str | None:
        disk = self.disk()
        return disk.path if disk else None

    @property
    def partitions_wanted(self) -> bool:
        """Some partitions, rather than the whole disk (whether or not any is ticked yet)."""
        return self.with_partitions and self.scope_seg.value() == "parts"

    def selection(self) -> tuple[int, ...]:
        """The ticked partitions' numbers; () for the whole disk."""
        if not self.partitions_wanted:
            return ()
        return tuple(p.number for check, p in self._checks if check.isChecked())

    def _selected(self) -> None:
        disk = self.disk()
        if self.with_partitions:
            self._fill_partitions(disk)
        self._describe(disk)
        self.changed.emit()

    def _describe(self, disk: devices.PhysicalDisk | None) -> None:
        if disk is None:
            self.desc.setText("")
            return
        parts = []
        for p in disk.partitions:
            what = " ".join(x for x in (p.fstype, f"“{p.label}”" if p.label else "") if x)
            parts.append(f"{p.name} {human_bytes(p.size)}" + (f" {what}" if what else ""))
        listed = parts and not self.partitions_wanted        # (else they are the ticks below)
        self.desc.setText(f"{disk.path} · {disk.summary}" + (f"\n{', '.join(parts)}" if listed else ""))

    def _fill_partitions(self, disk: devices.PhysicalDisk | None) -> None:
        # after Refresh, the same disk keeps its ticks
        keep = {p.number for c, p in self._checks if c.isChecked()} if disk and disk.path == self._checks_of else set()
        for check, _p in self._checks:
            check.deleteLater()
        self._checks = []
        self._checks_of = disk.path if disk else None
        choosable = [p for p in (disk.partitions if disk else []) if not p.extended]
        for p in choosable:
            check = QCheckBox(p.summary)
            if not p.number or p.start < 0:
                check.setEnabled(False)
                check.setToolTip("Where this partition is on the disk isn't known: copy the whole disk")
            check.setChecked(p.number in keep and check.isEnabled())
            check.toggled.connect(lambda _c: self.changed.emit())
            self.parts_lay.addWidget(check)
            self._checks.append((check, p))
        self.scope_seg.set_item_enabled("parts", bool(choosable),
                                        "Only the partitions you tick, in a new disk just their size (only they are "
                                        "read)" if choosable else "This disk has no partitions to choose from")
        if not choosable:
            self.scope_seg.set_value("whole")
        self._scope_changed(emit=False)

    def _scope_changed(self, emit: bool = True) -> None:
        self.parts_box.setVisible(self.partitions_wanted)
        if emit:
            self._describe(self.disk())
            self.changed.emit()


class CreateDiskDialog(QDialog):
    """Asks for a new disk. After exec() returns true, .disk is the NewDisk to create, or .job
    the copy of a physical disk to make (run with convert.execute)."""

    def __init__(self, parent: QWidget, qemu: QemuImg | None, settings: QSettings):
        super().__init__(parent)
        self.qemu = qemu
        self.settings = settings
        self.disk: NewDisk | None = None
        self.job: Job | None = None
        self.plan: CreatePlan | Plan | None = None
        self._overwrite_ok: Path | None = None     # the file the save dialog said may be replaced
        self._auto_name = True                     # the name follows the source until it's edited
        self._device_info: dict[tuple[str, tuple[int, ...]], ImageInfo] = {}
        self.setWindowTitle("New disk")
        self.setMinimumWidth(600)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._replan)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 18, 20, 16)
        title = QLabel("Create a virtual disk")
        title.setObjectName("Title")
        title.setFont(scaled_font(title, 1.3, bold=True))
        lay.addWidget(title)
        intro = QLabel("An empty disk to install an operating system on, one with a formatted partition to "
                       "attach to a virtual machine as an extra drive, or a copy of one of this computer's "
                       "disks to run in a virtual machine.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnMinimumWidth(0, 86)
        grid.setColumnStretch(1, 1)
        row = 0

        def label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setObjectName("Muted")
            return lbl

        self.source_seg = SegmentedControl()
        for key, text, tip in SOURCES:
            self.source_seg.add(text, key, tip)
        self.source_seg.set_value("new")
        self.source_seg.changed.connect(lambda _v: self._source_changed())
        grid.addWidget(label("Start from"), row, 0)
        grid.addWidget(self.source_seg, row, 1)
        row += 1

        self.disk_label = label("Disk")
        self.chooser = DiskChooser(partitions=True)
        self.chooser.changed.connect(self._disk_changed)
        grid.addWidget(self.disk_label, row, 0, Qt.AlignTop)
        grid.addWidget(self.chooser, row, 1)
        row += 1

        self.picker = TargetPicker(label_width=0, stream=False)
        self.picker.variant_label.hide()        # the row sits under the format grid
        self.picker.changed.connect(self._format_changed)
        grid.addWidget(label("Format"), row, 0, Qt.AlignTop)
        grid.addWidget(self.picker, row, 1)
        row += 1

        self.size_label = label("Size")
        size_row = QHBoxLayout()
        size_row.setSpacing(8)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 999999)
        self.size_spin.setValue(64)
        self.size_spin.setMinimumWidth(110)
        self.size_spin.valueChanged.connect(lambda _v: self._changed())
        self.unit_box = QComboBox()
        for name, factor in UNITS:
            self.unit_box.addItem(name, factor)
        self.unit_box.setCurrentIndex(1)
        self.unit_box.currentIndexChanged.connect(lambda _i: self._changed())
        size_row.addWidget(self.size_spin)
        size_row.addWidget(self.unit_box)
        size_row.addStretch(1)
        self.size_box = QWidget()
        self.size_box.setLayout(size_row)
        size_row.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.size_label, row, 0)
        grid.addWidget(self.size_box, row, 1)
        row += 1

        self.contents_label = label("Contents")
        self.contents_seg = SegmentedControl()
        for key, text, tip in CONTENTS:
            self.contents_seg.add(text, key, tip)
        self.contents_seg.set_value("empty")
        self.contents_seg.changed.connect(lambda _v: self._contents_changed())
        grid.addWidget(self.contents_label, row, 0)
        grid.addWidget(self.contents_seg, row, 1)
        row += 1

        self.table_label = label("Partitions")
        self.table_seg = SegmentedControl()
        self.table_seg.add("GPT", "gpt", "For UEFI machines and disks over 2 TB; every current system reads it")
        self.table_seg.add("MBR", "mbr", "The older table, for BIOS machines and old systems (up to 2 TB)")
        self.table_seg.set_value("gpt")
        self.table_seg.changed.connect(lambda _v: self._changed())
        grid.addWidget(self.table_label, row, 0)
        grid.addWidget(self.table_seg, row, 1)
        row += 1

        self.fs_label = label("File system")
        fs_row = QHBoxLayout()
        fs_row.setSpacing(8)
        fs_row.setContentsMargins(0, 0, 0, 0)
        self.fs_box = QComboBox()
        tools = available_filesystems()
        for fs in FILESYSTEMS:
            text = fs.label if tools[fs.key] else f"{fs.label} (not installed)"
            self.fs_box.addItem(text, fs.key)
            self.fs_box.setItemData(self.fs_box.count() - 1, fs.title, Qt.ToolTipRole)
        self.fs_box.currentIndexChanged.connect(lambda _i: self._changed())
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Name (optional)")
        self.label_edit.textChanged.connect(lambda _t: self._changed())
        fs_row.addWidget(self.fs_box, 1)
        fs_row.addWidget(self.label_edit, 1)
        self.fs_row = QWidget()
        self.fs_row.setLayout(fs_row)
        grid.addWidget(self.fs_label, row, 0)
        grid.addWidget(self.fs_row, row, 1)
        row += 1
        self.fs_desc = QLabel()
        self.fs_desc.setObjectName("Muted")
        self.fs_desc.setWordWrap(True)
        grid.addWidget(self.fs_desc, row, 1)
        row += 1

        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        self.out_edit = QLineEdit()
        self.out_edit.textEdited.connect(self._name_typed)
        self.out_btn = QToolButton()
        self.out_btn.setObjectName("Flat")
        self.out_btn.setToolTip("Choose where to save the new disk")
        self.out_btn.setIcon(icons.icon("folder", theme.palette.text_muted))
        self.out_btn.clicked.connect(self._choose_output)
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(self.out_btn)
        grid.addWidget(label("Save as"), row, 0)
        grid.addLayout(out_row, row, 1)
        row += 1
        self.verify_chk = QCheckBox("Compare the copy with the disk afterwards (reads the disk twice)")
        self.verify_chk.setChecked(True)
        self.verify_chk.toggled.connect(lambda _c: self._changed())
        grid.addWidget(self.verify_chk, row, 1)
        lay.addLayout(grid)

        self.banner = QLabel()
        self.banner.setObjectName("Banner")
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.RichText)
        lay.addWidget(self.banner)

        buttons = QDialogButtonBox()
        self.create_btn = QPushButton("Create")
        self.create_btn.setProperty("variant", "primary")
        self.create_btn.setDefault(True)
        buttons.addButton(self.create_btn, QDialogButtonBox.AcceptRole)
        buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._restore()
        self._source_changed()

    # ------------------------------------------------------------------ state

    @property
    def physical(self) -> bool:
        return self.source_seg.value() == "physical"

    def _restore(self) -> None:
        s = self.settings
        self.picker.set_target(target_from_setting(str(s.value("create/target", "vdi")), F.VDI))
        try:
            self.size_spin.setValue(int(s.value("create/size", 64)))
            self.unit_box.setCurrentIndex(min(2, max(0, int(s.value("create/unit", 1)))))
        except (TypeError, ValueError):
            pass
        self.contents_seg.set_value(str(s.value("create/contents", "empty")))
        self.table_seg.set_value(str(s.value("create/table", "gpt")))
        index = self.fs_box.findData(str(s.value("create/fs", "ntfs")))
        self.fs_box.setCurrentIndex(max(0, index))
        self.source_seg.set_value(str(s.value("create/source", "new")))
        last = str(s.value("create/last_output", "") or "")
        self.folder = Path(last).parent if last else Path(str(s.value("last_dir", str(Path.home()))))
        if not self.folder.is_dir():
            self.folder = Path.home()
        self._name_for_source()

    def _save(self) -> None:
        s = self.settings
        variant = self.picker.variant_key()
        s.setValue("create/target", self.picker.target().format.key + (f":{variant}" if variant else ""))
        s.setValue("create/source", self.source_seg.value())
        if not self.physical:
            s.setValue("create/size", self.size_spin.value())
            s.setValue("create/unit", self.unit_box.currentIndex())
            s.setValue("create/contents", self.contents_seg.value())
            s.setValue("create/table", self.table_seg.value())
            s.setValue("create/fs", self.fs_box.currentData())

    @staticmethod
    def _free_name(path: Path) -> Path:
        try:
            if not path.exists():
                return path
        except OSError:
            return path
        stem, ext = path.name[:-len(path.suffix)] if path.suffix else path.name, path.suffix
        for n in range(2, 1000):
            candidate = path.with_name(f"{stem} {n}{ext}")
            if not candidate.exists():
                return candidate
        return path

    def output(self) -> Path:
        text = os.path.expanduser(self.out_edit.text().strip())
        path = Path(text) if text else Path.home() / "New disk"
        return path if path.is_absolute() else Path.home() / path

    def new_disk(self) -> NewDisk:
        contents = self.contents_seg.value()
        out = self.output()
        return NewDisk(output=out, target=self.picker.target(),
                       size=self.size_spin.value() * int(self.unit_box.currentData()),
                       scheme="none" if contents == "empty" else self.table_seg.value(),
                       filesystem=self.fs_box.currentData() if contents == "formatted" else "",
                       label=self.label_edit.text(), overwrite=out == self._overwrite_ok)

    def copy_job(self) -> Job | None:
        path = self.chooser.path()
        if path is None:
            return None
        out = self.output()
        return Job(source=Path(path), target=self.picker.target(), output=out, verify=self.verify_chk.isChecked(),
                   overwrite=out == self._overwrite_ok, elevate="pkexec", partitions=self.chooser.selection())

    # ------------------------------------------------------------------ reacting

    def _name_for_source(self) -> None:
        """A name from the source (the disk's model, or "New disk") until one is typed."""
        if not self._auto_name:
            return
        disk = self.chooser.disk() if self.physical else None
        stem = disk.stem if disk is not None else "New disk"
        if disk is not None and self.chooser.selection():
            stem = devices.partition_stem(disk, self.chooser.selection())
        self.out_edit.setText(str(self._free_name(self.folder / f"{stem}{self.picker.target().format.extension}")))

    def _name_typed(self, _text: str) -> None:
        self._auto_name = False
        self._changed()

    def _source_changed(self) -> None:
        physical = self.physical
        if physical and not self.chooser.disks:
            self.chooser.refresh()
        for w in (self.disk_label, self.chooser, self.verify_chk):
            w.setVisible(physical)
        for w in (self.size_label, self.size_box, self.contents_label, self.contents_seg):
            w.setVisible(not physical)
        self.create_btn.setText("Copy the disk" if physical else "Create")
        self._name_for_source()
        self._contents_changed()

    def _disk_changed(self) -> None:
        self._name_for_source()
        self._changed()
        if self.isVisible():
            QTimer.singleShot(0, self._fit)      # the partitions' ticks come and go

    def _format_changed(self) -> None:
        fmt = self.picker.target().format
        out = self.output()
        known = F.by_extension(out.name)
        if known is not None and known is not fmt and known.writable:
            # follow the format with the extension (New disk.vdi -> New disk.vhdx)
            stem = out.name[:-len(out.suffix)]
            self.out_edit.setText(str(self._free_name(out.with_name(stem + fmt.extension))))
        self._changed()

    def _contents_changed(self) -> None:
        contents = None if self.physical else self.contents_seg.value()
        for w in (self.table_label, self.table_seg):
            w.setVisible(contents in ("partitioned", "formatted"))
        for w in (self.fs_label, self.fs_row, self.fs_desc):
            w.setVisible(contents == "formatted")
        self._changed()
        if self.isVisible():
            QTimer.singleShot(0, self._fit)

    def _fit(self) -> None:
        """As tall as the rows need (they come and go with the choices), at least 600 wide."""
        self.layout().activate()
        width = max(600, self.sizeHint().width())
        height = self.heightForWidth(width)
        self.resize(width, height if height > 0 else self.sizeHint().height())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._fit()

    def _changed(self) -> None:
        key = self.fs_box.currentData()
        fs = next((f for f in FILESYSTEMS if f.key == key), None)
        self.fs_desc.setText(fs.title if fs else "")
        self._timer.start()

    def _plan_copy(self) -> tuple[Plan | None, str, list[str]]:
        job = self.copy_job()
        if job is None:
            return None, "No disk chosen", [self.chooser.error or "No disks were found."]
        if self.chooser.partitions_wanted and not job.partitions:
            return None, "No partitions chosen", ["Tick the partitions to copy."]
        key = (str(job.source), job.partitions)
        info = self._device_info.get(key)
        if info is None:
            info = self._device_info[key] = devices.inspect_device(job.source, job.partitions)
        plan = make_plan(job, self.qemu, info)
        summary = f"{human_bytes(plan.size)} {plan.target.label}, a copy of {info.name}"
        return plan, summary, plan.errors

    def _replan(self) -> None:
        try:
            if self.physical:
                self.plan, summary, errors = self._plan_copy()
            else:
                self.plan = make_create_plan(self.new_disk(), self.qemu)
                summary, errors = self.plan.summary, self.plan.errors
        except (OSError, ConvertError) as exc:      # a name the file system refuses, lsblk failing…
            self.plan, summary = None, "Can't continue"
            errors = [getattr(exc, "strerror", None) and f"{exc.strerror}: {exc.filename}" or str(exc)]
        pal = theme.palette
        lines = [f"<b>{html.escape(summary)}</b>"]
        lines += [f'<span style="color:{pal.danger}">✕</span> {html.escape(e)}' for e in errors]
        if self.plan is not None:
            lines += [f'<span style="color:{pal.warning}">!</span> {html.escape(w)}' for w in self.plan.warnings]
            lines += [f'<span style="color:{pal.accent}">•</span> {html.escape(n)}' for n in self.plan.notes]
        self.banner.setText("<br>".join(lines))
        self.create_btn.setEnabled(self.plan is not None and self.plan.ok)

    def _choose_output(self) -> None:
        fmt = self.picker.target().format
        path, _ = QFileDialog.getSaveFileName(self, "Save the new disk as", str(self.output()),
                                              f"{fmt.label} (*{fmt.extension});;All files (*)")
        if not path:
            return
        chosen = Path(path)
        # the save dialog asked whether to replace the file it returned (not one with an added extension)
        self._overwrite_ok = chosen if chosen.exists() else None
        out = chosen if chosen.suffix else chosen.with_name(chosen.name + fmt.extension)
        self._auto_name = False
        self.folder = out.parent
        self.out_edit.setText(str(out))
        self._changed()

    def _create(self) -> None:
        self._timer.stop()
        self._replan()
        if self.plan is None or not self.plan.ok:
            return
        if self.physical:
            self.job = self.plan.job
        else:
            self.disk = self.plan.disk
        self._save()
        self.accept()


class DiskPickerDialog(QDialog):
    """Choose one of the computer's disks (to put it in the list of images to convert)."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Add a physical disk")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 18, 20, 16)
        title = QLabel("Add a physical disk")
        title.setObjectName("Title")
        title.setFont(scaled_font(title, 1.3, bold=True))
        lay.addWidget(title)
        intro = QLabel("The disk goes into the list like an image file, and is converted into a virtual disk "
                       "with everything on it, or with just the partitions you tick. It is only read, never "
                       "written to; you'll be asked for your password to read it.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.chooser = DiskChooser(partitions=True)
        lay.addWidget(self.chooser)
        buttons = QDialogButtonBox()
        self.add_btn = QPushButton("Add")
        self.add_btn.setProperty("variant", "primary")
        self.add_btn.setDefault(True)
        buttons.addButton(self.add_btn, QDialogButtonBox.AcceptRole)
        buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.chooser.changed.connect(lambda: self.add_btn.setEnabled(
            self.chooser.path() is not None and (not self.chooser.partitions_wanted or bool(self.chooser.selection()))))
        self.chooser.refresh()
