"""The New disk dialog: format, size, and what goes on the disk."""

from __future__ import annotations

import html
import os
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import (
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

from ..core import formats as F
from ..core.create import FILESYSTEMS, CreatePlan, NewDisk, available_filesystems, make_create_plan
from ..core.qemu import QemuImg
from ..core.util import GIB, MIB, TIB
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


class CreateDiskDialog(QDialog):
    """Asks for a new disk; .disk is the NewDisk to create after exec() returns true."""

    def __init__(self, parent: QWidget, qemu: QemuImg | None, settings: QSettings):
        super().__init__(parent)
        self.qemu = qemu
        self.settings = settings
        self.disk: NewDisk | None = None
        self.plan: CreatePlan | None = None
        self._overwrite_ok: Path | None = None     # a file the save dialog said may be replaced
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
        intro = QLabel("An empty disk to install an operating system on, or one with a formatted partition "
                       "to attach to a virtual machine as an extra drive.")
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

        self.picker = TargetPicker(label_width=0, stream=False)
        self.picker.variant_label.hide()        # the row sits under the format grid
        self.picker.changed.connect(self._format_changed)
        grid.addWidget(label("Format"), row, 0, Qt.AlignTop)
        grid.addWidget(self.picker, row, 1)
        row += 1

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
        grid.addWidget(label("Size"), row, 0)
        grid.addLayout(size_row, row, 1)
        row += 1

        self.contents_seg = SegmentedControl()
        for key, text, tip in CONTENTS:
            self.contents_seg.add(text, key, tip)
        self.contents_seg.set_value("empty")
        self.contents_seg.changed.connect(lambda _v: self._contents_changed())
        grid.addWidget(label("Contents"), row, 0)
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
        grid.addWidget(self.fs_label, row, 0)
        grid.addLayout(fs_row, row, 1)
        row += 1
        self.fs_desc = QLabel()
        self.fs_desc.setObjectName("Muted")
        self.fs_desc.setWordWrap(True)
        grid.addWidget(self.fs_desc, row, 1)
        row += 1

        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        self.out_edit = QLineEdit()
        self.out_edit.textEdited.connect(lambda _t: self._changed())
        self.out_btn = QToolButton()
        self.out_btn.setObjectName("Flat")
        self.out_btn.setToolTip("Choose where to save the new disk")
        self.out_btn.setIcon(icons.icon("folder", theme.palette.text_muted))
        self.out_btn.clicked.connect(self._choose_output)
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(self.out_btn)
        grid.addWidget(label("Save as"), row, 0)
        grid.addLayout(out_row, row, 1)
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
        self._contents_changed()

    # ------------------------------------------------------------------ state

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
        last = str(s.value("create/last_output", "") or "")
        folder = Path(last).parent if last else Path(str(s.value("last_dir", str(Path.home()))))
        if not folder.is_dir():
            folder = Path.home()
        self.out_edit.setText(str(self._free_name(folder / f"New disk{self.picker.target().format.extension}")))

    def _save(self) -> None:
        s = self.settings
        variant = self.picker.variant_key()
        s.setValue("create/target", self.picker.target().format.key + (f":{variant}" if variant else ""))
        s.setValue("create/size", self.size_spin.value())
        s.setValue("create/unit", self.unit_box.currentIndex())
        s.setValue("create/contents", self.contents_seg.value())
        s.setValue("create/table", self.table_seg.value())
        s.setValue("create/fs", self.fs_box.currentData())

    @staticmethod
    def _free_name(path: Path) -> Path:
        if not path.exists():
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

    # ------------------------------------------------------------------ reacting

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
        contents = self.contents_seg.value()
        for w in (self.table_label, self.table_seg):
            w.setVisible(contents != "empty")
        for w in (self.fs_label, self.fs_box, self.label_edit, self.fs_desc):
            w.setVisible(contents == "formatted")
        self._changed()
        if self.isVisible():
            QTimer.singleShot(0, self._fit)

    def _fit(self) -> None:
        """As tall as the rows need (they come and go with Contents), at least 600 wide."""
        hint = self.sizeHint()
        self.resize(max(600, hint.width()), hint.height())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._fit()

    def _changed(self) -> None:
        key = self.fs_box.currentData()
        fs = next((f for f in FILESYSTEMS if f.key == key), None)
        self.fs_desc.setText(fs.title if fs else "")
        self._timer.start()

    def _replan(self) -> None:
        self.plan = make_create_plan(self.new_disk(), self.qemu)
        pal = theme.palette
        lines = [f"<b>{html.escape(self.plan.summary)}</b>"]
        lines += [f'<span style="color:{pal.danger}">✕</span> {html.escape(e)}' for e in self.plan.errors]
        lines += [f'<span style="color:{pal.warning}">!</span> {html.escape(w)}' for w in self.plan.warnings]
        lines += [f'<span style="color:{pal.accent}">•</span> {html.escape(n)}' for n in self.plan.notes]
        self.banner.setText("<br>".join(lines))
        self.create_btn.setEnabled(self.plan.ok)

    def _choose_output(self) -> None:
        fmt = self.picker.target().format
        path, _ = QFileDialog.getSaveFileName(self, "Save the new disk as", str(self.output()),
                                              f"{fmt.label} (*{fmt.extension});;All files (*)")
        if not path:
            return
        out = Path(path)
        if not out.suffix:
            out = out.with_name(out.name + fmt.extension)
        # the save dialog already asked whether to replace it
        self._overwrite_ok = out if out.exists() else None
        self.out_edit.setText(str(out))
        self._changed()

    def _create(self) -> None:
        self._timer.stop()
        self._replan()
        if self.plan is None or not self.plan.ok:
            return
        self.disk = self.plan.disk
        self._save()
        self.accept()
