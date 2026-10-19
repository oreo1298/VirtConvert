"""The Write to disk dialog: a virtual disk onto one of the computer's physical disks."""

from __future__ import annotations

import html
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEventLoop, QSettings, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import devices
from ..core.errors import ConvertError
from ..core.images import expand_sources, inspect_image
from ..core.probe import ImageInfo
from ..core.qemu import QemuImg
from ..core.util import human_bytes
from ..core.writedisk import DiskWrite, WritePlan, disk_identity, make_write_plan
from .create_dialog import DiskChooser
from .dialogs import ask_yes_no
from .theme import theme
from .widgets import breakable, scaled_font


class WriteDiskDialog(QDialog):
    """Asks which disk to write a virtual disk onto, and whether everything on it may be erased.
    After exec() returns true, .job is the DiskWrite to run (writedisk.write_to_disk)."""

    def __init__(self, parent: QWidget, qemu: QemuImg | None, settings: QSettings, source: Path | None = None,
                 info: ImageInfo | None = None, member: str | None = None,
                 parents: dict[Path, Path] | None = None, force_parent: bool = False,
                 image_filter: str = "All files (*)", partitions: tuple[int, ...] = ()):
        super().__init__(parent)
        self.qemu = qemu
        self.settings = settings
        self.source, self.info, self.member = source, info, member
        self.parents, self.force_parent = dict(parents or {}), force_parent
        self.partitions = partitions        # some partitions of a physical disk SOURCE
        self.image_filter = image_filter
        self.job: DiskWrite | None = None
        self.plan: WritePlan | None = None
        self.setWindowTitle("Write to disk")
        self.setMinimumWidth(600)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._replan)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 18, 20, 16)
        title = QLabel("Write to a physical disk")
        title.setObjectName("Title")
        title.setFont(scaled_font(title, 1.3, bold=True))
        lay.addWidget(title)
        intro = QLabel("Puts a virtual disk onto one of this computer's disks (a USB stick, a memory card, an SSD "
                       "or a hard disk), so a computer can start from it. Everything on that disk is erased.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnMinimumWidth(0, 86)
        grid.setColumnStretch(1, 1)

        def label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setObjectName("Muted")
            return lbl

        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        self.source_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.source_btn = QPushButton("Choose…")
        self.source_btn.setToolTip("Choose another virtual disk")
        self.source_btn.clicked.connect(self._choose_source)
        source_row.addWidget(self.source_label, 1)
        source_row.addWidget(self.source_btn, 0, Qt.AlignTop)
        grid.addWidget(label("Virtual disk"), 0, 0, Qt.AlignTop)
        grid.addLayout(source_row, 0, 1)

        self.chooser = DiskChooser(for_writing=True)
        self.chooser.changed.connect(self._changed)
        grid.addWidget(label("Write onto"), 1, 0, Qt.AlignTop)
        grid.addWidget(self.chooser, 1, 1)

        self.verify_chk = QCheckBox("Read the disk back afterwards and compare it")
        self.verify_chk.setToolTip("Checks that every byte on the disk is what was written; takes about as long "
                                   "again")
        self.verify_chk.setChecked(settings.value("write/verify", True, type=bool))
        grid.addWidget(self.verify_chk, 2, 1)
        lay.addLayout(grid)

        self.banner = QLabel()
        self.banner.setObjectName("Banner")
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.RichText)
        lay.addWidget(self.banner)

        buttons = QDialogButtonBox()
        self.write_btn = QPushButton("Write…")
        self.write_btn.setProperty("variant", "danger")
        buttons.addButton(self.write_btn, QDialogButtonBox.AcceptRole)
        cancel = buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        cancel.setDefault(True)          # Enter never erases a disk
        buttons.accepted.connect(self._write)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._show_source()
        self.chooser.refresh()

    # ------------------------------------------------------------------ the source

    def _show_source(self) -> None:
        info = self.info
        if self.source is None:
            self.source_label.setText("No virtual disk chosen")
        elif info is None or info.format is None:
            self.source_label.setText(html.escape(breakable(self.source.name)))
        else:
            self.source_label.setText(f"<b>{html.escape(breakable(info.name))}</b><br>"
                                      f"{html.escape(info.label)} · {human_bytes(info.virtual_size)}"
                                      + (f" · {html.escape(info.partitions.label)}" if info.partitions else ""))
        self.source_label.setToolTip(str(self.source) if self.source else "")

    def _choose_source(self) -> None:
        start = str(self.source.parent) if self.source else str(self.settings.value("last_dir", str(Path.home())))
        path, _ = QFileDialog.getOpenFileName(self, "The virtual disk to write", start, self.image_filter)
        if not path:
            return
        sources = expand_sources(path)
        member = None
        if len(sources) > 1:          # an appliance with several disks: which one?
            names = [m or p.name for p, m in sources]
            name, ok = QInputDialog.getItem(self, "Which disk?", f"{Path(path).name} holds {len(names)} disks. "
                                            "Which one should be written?", names, 0, False)
            if not ok:
                return
            path, member = sources[names.index(name)]
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        self.setEnabled(False)
        try:
            # (in a thread: a chain or an appliance can take a moment, and the window stays alive)
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(inspect_image, Path(path), self.qemu, member=member)
                while not future.done():
                    QCoreApplication.processEvents(QEventLoop.AllEvents, 50)
                    time.sleep(0.01)
            self.source, self.member, self.parents, self.force_parent = Path(path), member, {}, False
            self.partitions = ()
            self.info = future.result()
        finally:
            self.setEnabled(True)
            QGuiApplication.restoreOverrideCursor()
        self._show_source()
        self._changed()

    # ------------------------------------------------------------------ planning

    def _changed(self) -> None:
        self._timer.start()

    def job_for(self, disk: devices.PhysicalDisk) -> DiskWrite:
        return DiskWrite(source=self.source, disk=Path(disk.path), verify=self.verify_chk.isChecked(),
                         member=self.member, parents=dict(self.parents), force_parent=self.force_parent,
                         elevate="pkexec", expect=disk_identity(disk), partitions=self.partitions)

    def _replan(self, fresh: bool = False) -> None:
        """FRESH: ask lsblk about the chosen disk again (before erasing it), instead of the list's
        description from when it was filled."""
        disk = self.chooser.disk()
        self.plan = None
        errors: list[str] = []
        summary = "Nothing to write"
        if self.source is None:
            errors = ["Choose the virtual disk to write."]
        elif disk is None:
            errors = [self.chooser.error or "No disks were found."]
        else:
            try:
                now = devices.disk_info(disk.path) if fresh else disk
                self.plan = make_write_plan(self.job_for(disk), self.qemu, self.info, now)
                summary, errors = self.plan.summary, self.plan.errors
            except (OSError, ConvertError) as exc:
                summary, errors = "Can't continue", [str(exc)]
        pal = theme.palette
        lines = [f"<b>{html.escape(summary)}</b>"]
        lines += [f'<span style="color:{pal.danger}">✕</span> {html.escape(e)}' for e in errors]
        if self.plan is not None:
            lines += [f'<span style="color:{pal.warning}">!</span> {html.escape(w)}' for w in self.plan.warnings]
            lines += [f'<span style="color:{pal.accent}">•</span> {html.escape(n)}' for n in self.plan.notes]
        self.banner.setText("<br>".join(lines))
        self.write_btn.setEnabled(self.plan is not None and self.plan.ok)
        if self.isVisible():
            QTimer.singleShot(0, self._fit)

    def _fit(self) -> None:
        """As tall as the wrapped text needs at this width."""
        self.layout().activate()
        width = max(600, self.width())
        height = self.heightForWidth(width)
        self.resize(width, height if height > 0 else self.sizeHint().height())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._timer.stop()
        self._replan()

    def _write(self) -> None:
        self._timer.stop()
        self._replan(fresh=True)
        plan = self.plan
        if plan is None or not plan.ok or plan.disk is None:
            return
        disk = plan.disk
        if not ask_yes_no(self, "Erase the disk", f"Erase everything on {disk.title}?", yes="Erase and write",
                          danger=True, default_no=True,
                          informative=f"{disk.path}, {human_bytes(disk.size)}. On it now: {plan.erased}.\n\n"
                                      f"{plan.info.name} is written onto it instead. This can't be undone."):
            return
        self.settings.setValue("write/verify", self.verify_chk.isChecked())
        self.job = plan.job
        self.accept()
