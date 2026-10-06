"""The main window."""

from __future__ import annotations

import html
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..core import formats as F
from ..core import system
from ..core.convert import (
    Job,
    Target,
    default_output,
    execute,
    make_plan,
    repair,
    verify_images,
)
from ..core.create import create_disk, make_create_plan
from ..core.errors import ConvertError, OperationCancelled, VerifyError, os_error_text
from ..core.images import expand_sources, inspect_image
from ..core.qemu import QemuImg
from ..core.util import human_bytes, mmss
from ..core.writedisk import make_write_plan, write_to_disk
from . import icons
from .create_dialog import CreateDiskDialog, DiskPickerDialog
from .dialogs import AboutDialog, QemuMissingDialog, ask_yes_no
from .pickers import TargetPicker, target_from_setting
from .queue import (
    BLOCKED,
    CANCELLED,
    DONE,
    FAILED,
    INSPECTING,
    READY,
    RUNNING,
    STARTABLE,
    WAITING,
    Item,
    QueueModel,
    StatusDelegate,
)
from .theme import mono_font, theme
from .widgets import (
    Card,
    DropZone,
    KeyValueGrid,
    StatTile,
    StatusDot,
    Toast,
    badge,
    breakable,
    scaled_font,
    set_badge,
)
from .worker import OperationRunner
from .write_dialog import WriteDiskDialog

MAX_LOG_ENTRIES = 3000
IMAGE_FILTER = ("Disk images (" + " ".join(f"*{e}" for f in F.ALL for e in f.extensions) + ");;"
                "All files (*)")


class ClickableFrame(QFrame):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    inspected = Signal(int, object)
    logLine = Signal(str, str)

    def __init__(self, qemu: QemuImg | None = None):
        super().__init__()
        self.qemu = qemu
        self.settings = QSettings("virtconvert", "virtconvert")
        self.model = QueueModel(self)
        self.runner = OperationRunner(self)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="inspect")
        self._log_entries: list[tuple[str, str, str]] = []
        self._icon_actions: list[tuple[QAction, str, str]] = []
        self._running: Item | None = None
        self._stage = ""
        self._stage_t0 = 0.0
        self._stop_queue = False
        self._updating_controls = False
        self.output_folder: Path | None = None
        self._shown_target = Target(F.VDI)

        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.resize(1320, 840)
        self.setMinimumSize(1020, 660)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self.toast = Toast(self.centralWidget())

        self.inspected.connect(self._on_inspected)
        self.logLine.connect(self.log)
        self.runner.progress.connect(self._on_progress)
        self.runner.finished.connect(self._on_finished)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._selection_changed())
        self.model.rowsInserted.connect(lambda *_: self._queue_changed())
        self.model.rowsRemoved.connect(lambda *_: self._queue_changed())
        theme.changed.connect(self._retheme)

        self._restore_settings()
        self._retheme(theme.palette)
        self._queue_changed()
        self._selection_changed()
        self.log(f"{APP_NAME} {__version__} ready.")
        self._refresh_qemu_state(announce=True)

    # ------------------------------------------------------------------ construction

    def _action(self, text: str, icon: str | None, slot, shortcut=None, tip: str = "",
                checkable: bool = False) -> QAction:
        act = QAction(text, self)
        if shortcut:
            seqs = shortcut if isinstance(shortcut, (list, tuple)) else [shortcut]
            act.setShortcuts([QKeySequence(s) for s in seqs])
        tooltip = tip or text.replace("…", "")
        if shortcut:
            first = shortcut[0] if isinstance(shortcut, (list, tuple)) else shortcut
            tooltip += f"  ({QKeySequence(first).toString(QKeySequence.NativeText)})"
        act.setToolTip(tooltip)
        act.setStatusTip(tip or text)
        act.setCheckable(checkable)
        if slot is not None:
            act.triggered.connect(slot)
        if icon:
            self._icon_actions.append((act, icon, "text"))
        self.addAction(act)
        return act

    def _build_actions(self) -> None:
        a = self._action
        self.act_add = a("Add…", "open", lambda: self.add_files(), "Ctrl+O",
                         "Add VHD, VHDX, VDI, VMDK, QCOW2, OVA or other disk images to the list")
        self.act_new = a("New disk…", "plus", self.new_disk, "Ctrl+N",
                         "Create a new virtual disk: empty, formatted, or a copy of a physical disk")
        self.act_add_disk = a("Add physical disk…", "hdd", self.add_physical_disk, "Ctrl+Shift+D",
                              "Put one of this computer's disks in the list, to convert it into a virtual disk")
        self.act_write = a("Write to disk…", "drop", self.write_to_disk, "Ctrl+Shift+W",
                           "Put a virtual disk onto a USB stick, memory card, SSD or hard disk (erases it)")
        self.act_add_images = QAction("Add images…", self)
        self.act_add_images.triggered.connect(lambda: self.add_files())
        self.act_remove = a("Remove", "trash", self.remove_selected, "Del",
                            "Remove the selected images from the list (the files stay)")
        self.act_clear = a("Clear done", "eraser", self.clear_finished, "Ctrl+Shift+L",
                           "Remove the converted images from the list")
        self.act_convert = a("Convert", "play", self.convert_all, ["F5", "Ctrl+Return"],
                             "Convert every image that is ready")
        self.act_cancel = a("Cancel", "cancel", self.cancel_operation, "Esc",
                            "Stop converting (the unfinished file is removed)")
        self.act_cancel.setEnabled(False)
        self.act_verify = a("Compare…", "shield_check", self.compare_files, "Ctrl+K",
                            "Check that two images contain the same disk")
        self.act_folder = a("Show file", "folder", self.show_output, "Ctrl+Shift+O",
                            "Open the folder of the converted file")
        self.act_parent = a("Choose parent…", "link", self.choose_parent, None,
                            "Pick the parent of a checkpoint or snapshot by hand")
        self.act_repair = a("Repair…", "wrench", self.repair_selected, None,
                            "Apply the log of a VHDX that wasn't closed cleanly")
        self.act_copy_path = a("Copy output path", "copy", self.copy_output_path, None)
        self.act_install_qemu = a("Install qemu-img…", "plus", self.offer_qemu_install, None,
                                  "Install the qemu-img package VirtConvert&Create needs")
        self.act_quit = a("Quit", None, self.close, "Ctrl+Q")
        self.act_about = a("About", "info", self.show_about, None)
        self.act_theme = a("Toggle dark/light", "moon", self.toggle_theme, "Ctrl+Shift+T")
        self.act_select_all = a("Select all", None, lambda: self.table.selectAll(), "Ctrl+A")

        self.theme_group = QActionGroup(self)
        self.theme_actions = {}
        for mode, label in (("system", "Follow system"), ("dark", "Dark"), ("light", "Light")):
            act = QAction(label, self, checkable=True)
            act.triggered.connect(lambda _c=False, m=mode: self.set_theme(m))
            self.theme_group.addAction(act)
            self.theme_actions[mode] = act

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        for act in (self.act_add, self.act_add_disk, self.act_new, self.act_write, None, self.act_folder,
                    self.act_copy_path, None, self.act_quit):
            m.addSeparator() if act is None else m.addAction(act)
        m = mb.addMenu("&Images")
        for act in (self.act_convert, self.act_cancel, None, self.act_remove, self.act_clear,
                    self.act_select_all, None, self.act_write, self.act_parent, self.act_repair, None,
                    self.act_verify):
            m.addSeparator() if act is None else m.addAction(act)
        m = mb.addMenu("&View")
        tm = m.addMenu("Theme")
        for act in self.theme_actions.values():
            tm.addAction(act)
        m.addAction(self.act_theme)
        m = mb.addMenu("&Help")
        m.addAction(self.act_install_qemu)
        m.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("MainToolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setIconSize(QSize(22, 22))
        tb.setContextMenuPolicy(Qt.PreventContextMenu)
        self.addToolBar(Qt.TopToolBarArea, tb)
        self.toolbar = tb

        brand = QWidget()
        bl = QHBoxLayout(brand)
        bl.setContentsMargins(4, 0, 14, 0)
        bl.setSpacing(10)
        self.brand_icon = QLabel()
        text = QVBoxLayout()
        text.setSpacing(0)
        name = QLabel(APP_NAME)
        name.setFont(scaled_font(name, 1.25, bold=True))
        sub = QLabel("Convert and create virtual disks")
        sub.setObjectName("Muted")
        sub.setFont(scaled_font(sub, 0.85))
        text.addWidget(name)
        text.addWidget(sub)
        bl.addWidget(self.brand_icon)
        bl.addLayout(text)
        tb.addWidget(brand)
        tb.addSeparator()
        # (Clear done stays in the Images menu and the list's menu: the toolbar has to fit 1020 pixels)
        for act in (self.act_add, self.act_new, self.act_remove):
            tb.addAction(act)
        add_btn = tb.widgetForAction(self.act_add)
        add_menu = QMenu(add_btn)
        add_menu.addAction(self.act_add_images)
        add_menu.addAction(self.act_add_disk)
        add_btn.setMenu(add_menu)
        add_btn.setPopupMode(QToolButton.MenuButtonPopup)
        tb.addSeparator()
        tb.addAction(self.act_convert)
        tb.addSeparator()
        for act in (self.act_verify, self.act_folder):
            tb.addAction(act)
        convert_btn = tb.widgetForAction(self.act_convert)
        convert_btn.setProperty("accent", True)
        convert_btn.setMinimumWidth(72)
        for act in tb.actions():
            w = tb.widgetForAction(act)
            if isinstance(w, QToolButton):
                w.setMinimumWidth(max(w.minimumWidth(), 58))
                w.setCursor(Qt.PointingHandCursor)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.qemu_pill = ClickableFrame()
        self.qemu_pill.setObjectName("Inset")
        pl = QHBoxLayout(self.qemu_pill)
        pl.setContentsMargins(8, 4, 12, 4)
        pl.setSpacing(4)
        self.qemu_dot = StatusDot(8)
        self.qemu_text = QLabel("Looking for qemu-img…")
        pl.addWidget(self.qemu_dot)
        pl.addWidget(self.qemu_text)
        self.qemu_pill.setCursor(Qt.PointingHandCursor)
        self.qemu_pill.clicked.connect(lambda: self.offer_qemu_install() if self.qemu is None
                                       else self.show_about())
        tb.addWidget(self.qemu_pill)

        self.theme_btn = QToolButton()
        self.theme_btn.setDefaultAction(self.act_theme)
        self.theme_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.theme_btn.setObjectName("Flat")
        tb.addWidget(self.theme_btn)
        self.about_btn = QToolButton()
        self.about_btn.setDefaultAction(self.act_about)
        self.about_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.about_btn.setObjectName("Flat")
        tb.addWidget(self.about_btn)

    def _build_central(self) -> None:
        page = QWidget()
        page.setObjectName("Page")
        outer = QHBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 10)
        outer.setSpacing(14)

        side = QWidget()
        side.setObjectName("Page")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 4, 0)
        sl.setSpacing(14)
        sl.addWidget(self._build_image_card())
        sl.addWidget(self._build_target_card())
        sl.addWidget(self._build_options_card())
        sl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(372)
        scroll.viewport().setObjectName("Page")
        outer.addWidget(scroll)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_queue_card())
        self.splitter.addWidget(self._build_log_card())
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([520, 200])
        outer.addWidget(self.splitter, 1)
        self.setCentralWidget(page)

    def _build_image_card(self) -> Card:
        card = Card("Image", "file_image")
        self.image_card = card
        self.img_badge = badge("", "Idle")
        self.img_badge.hide()
        card.header.addWidget(self.img_badge)

        self.img_empty = QLabel("Add a disk image (VHD, VHDX, VDI, VMDK, QCOW2, OVA…) to convert it, "
                                "or drop files on the window. New disk makes an empty or formatted one, "
                                "or a copy of a physical disk.")
        self.img_empty.setObjectName("Muted")
        self.img_empty.setWordWrap(True)
        card.add(self.img_empty)

        self.img_body = QWidget()
        body = QVBoxLayout(self.img_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(9)
        self.img_name = QLabel()
        self.img_name.setFont(scaled_font(self.img_name, 1.15, bold=True))
        self.img_name.setWordWrap(True)
        self.img_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.img_sub = QLabel()
        self.img_sub.setObjectName("Muted")
        self.img_sub.setWordWrap(True)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(self.img_name)
        col.addWidget(self.img_sub)
        body.addLayout(col)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.tile_size = StatTile("Disk size")
        self.tile_file = StatTile("File size")
        self.tile_format = StatTile("Format")
        for t in (self.tile_size, self.tile_file, self.tile_format):
            tiles.addWidget(t)
        body.addLayout(tiles)
        self.img_grid = KeyValueGrid()
        self.img_grid.add_row("parts", "Partitions")
        self.img_grid.add_row("boot", "Boots with")
        self.img_grid.add_row("chain", "Based on")
        self.img_grid.add_row("creator", "Created by")
        body.addWidget(self.img_grid)
        self.img_banner = QLabel()
        self.img_banner.setObjectName("BannerError")
        self.img_banner.setWordWrap(True)
        self.img_banner.setTextFormat(Qt.RichText)
        body.addWidget(self.img_banner)
        row = QHBoxLayout()
        self.parent_btn = QPushButton("Choose parent…")
        self.parent_btn.setProperty("variant", "primary")
        self.parent_btn.clicked.connect(self.choose_parent)
        self.repair_btn = QPushButton("Repair…")
        self.repair_btn.setProperty("variant", "primary")
        self.repair_btn.clicked.connect(self.repair_selected)
        self.write_btn = QPushButton("Write to disk…")
        self.write_btn.setToolTip(self.act_write.statusTip())
        self.write_btn.clicked.connect(self.write_to_disk)
        row.addWidget(self.parent_btn)
        row.addWidget(self.repair_btn)
        row.addWidget(self.write_btn)
        row.addStretch(1)
        body.addLayout(row)
        card.add(self.img_body)
        return card

    def _build_target_card(self) -> Card:
        card = Card("Convert to", "convert")
        self.target_card = card
        self.picker = TargetPicker()
        self.picker.changed.connect(self._target_edited)
        card.add(self.picker)
        # the controls, by the names the tests and the screenshot tool use
        self.fmt_seg, self.alloc_seg, self.vmdk_seg = self.picker.fmt_seg, self.picker.alloc_seg, self.picker.vmdk_seg
        self.azure_chk, self.split_chk = self.picker.azure_chk, self.picker.split_chk

        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        lbl = QLabel("Save as")
        lbl.setObjectName("Muted")
        lbl.setMinimumWidth(74)
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Output file")
        self.out_edit.editingFinished.connect(self._output_typed)
        self.out_btn = QToolButton()
        self.out_btn.setObjectName("Flat")
        self.out_btn.setToolTip("Choose where to save the converted file")
        self.out_btn.clicked.connect(self.choose_output)
        out_row.addWidget(lbl)
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(self.out_btn)
        card.add_layout(out_row)

        self.plan_banner = QLabel()
        self.plan_banner.setObjectName("Banner")
        self.plan_banner.setWordWrap(True)
        self.plan_banner.setTextFormat(Qt.RichText)
        self.plan_banner.hide()
        card.add(self.plan_banner)
        return card

    def _build_options_card(self) -> Card:
        card = Card("Options", "sliders")
        self.verify_chk = QCheckBox("Compare the copy with the source")
        self.verify_chk.setToolTip("Reads both disks after converting and checks that every byte "
                                   "is the same (takes about as long again)")
        self.verify_chk.setChecked(True)
        self.overwrite_chk = QCheckBox("Replace files that already exist")
        self.overwrite_chk.toggled.connect(lambda _c: self._replan_all())
        card.add(self.verify_chk)
        card.add(self.overwrite_chk)
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("Save in")
        lbl.setObjectName("Muted")
        lbl.setMinimumWidth(74)
        self.folder_box = QComboBox()
        self.folder_box.addItem("The source's folder", None)
        self.folder_box.addItem("Choose a folder…", "choose")
        self.folder_box.activated.connect(self._folder_choice)
        row.addWidget(lbl)
        row.addWidget(self.folder_box, 1)
        card.add_layout(row)
        return card

    def _build_queue_card(self) -> Card:
        card = Card("Images", "list")
        self.queue_count = QLabel()
        self.queue_count.setObjectName("Muted")
        card.header.insertWidget(2, self.queue_count)
        add = QPushButton("Add…")
        add.setProperty("variant", "ghost")
        add.clicked.connect(lambda: self.add_files())
        card.header.addWidget(add)
        card.body().setContentsMargins(1, 12, 1, 1)
        card.header.setContentsMargins(15, 0, 9, 0)

        self.queue_stack = QStackedWidget()
        self.drop_zone = DropZone(text="Hyper-V disks become VDI for VirtualBox; VirtualBox disks go to VHDX for "
                                       "Hyper-V; or pick VMDK, QCOW2, OVA and more. Checkpoints and snapshots are "
                                       "merged with their parents.", secondary="New disk…")
        self.drop_zone.addRequested.connect(lambda: self.add_files())
        self.drop_zone.secondaryRequested.connect(self.new_disk)
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(14, 4, 14, 14)
        hl.addWidget(self.drop_zone)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(4, StatusDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setIconSize(QSize(18, 18))
        self.table.setFrameShape(QFrame.NoFrame)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.doubleClicked.connect(lambda _i: self.show_output())
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 150), (2, 150), (3, 96)):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
            self.table.setColumnWidth(col, width)
        hh.setSectionResizeMode(4, QHeaderView.Interactive)
        self.table.setColumnWidth(4, 260)
        self.queue_stack.addWidget(holder)
        self.queue_stack.addWidget(self.table)
        card.add(self.queue_stack, 1)
        return card

    def _build_log_card(self) -> Card:
        card = Card("Activity", "log")
        clear = QPushButton("Clear")
        clear.setProperty("variant", "ghost")
        clear.clicked.connect(self._clear_log)
        card.header.addWidget(clear)
        card.body().setContentsMargins(1, 10, 1, 1)
        card.header.setContentsMargins(15, 0, 9, 0)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("Log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_ENTRIES)
        self.log_view.setFont(mono_font(9.5))
        card.add(self.log_view, 1)
        return card

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(10, 2, 10, 2)
        lay.setSpacing(12)
        self.op_dot = StatusDot(8)
        self.op_label = QLabel("Ready")
        self.op_label.setMinimumWidth(160)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(280)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.hide()
        self.op_detail = QLabel()
        self.op_detail.setObjectName("Muted")
        self.op_detail.setFont(mono_font(9.5))
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setProperty("variant", "danger")
        self.cancel_btn.clicked.connect(self.cancel_operation)
        self.cancel_btn.hide()
        lay.addWidget(self.op_dot)
        lay.addWidget(self.op_label)
        lay.addWidget(self.progress)
        lay.addWidget(self.op_detail)
        lay.addWidget(self.cancel_btn)
        lay.addStretch(1)
        sb.addWidget(box, 1)
        self.last_op = QLabel()
        self.last_op.setObjectName("Muted")
        sb.addPermanentWidget(self.last_op)

    # ------------------------------------------------------------------ theming

    def _retheme(self, pal) -> None:
        for act, name, token in self._icon_actions:
            act.setIcon(icons.icon(name, getattr(pal, token), pal.text_faint))
        self.act_theme.setIcon(icons.icon("sun" if pal.dark else "moon", pal.text, pal.text_faint))
        self.act_convert.setIcon(icons.icon("play", pal.accent_text, pal.text_faint))
        self.brand_icon.setPixmap(icons.pixmap("convert", pal.accent, 30))
        self.out_btn.setIcon(icons.icon("folder", pal.text_muted))
        btn = self.toolbar.widgetForAction(self.act_convert)
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        for mode, act in self.theme_actions.items():
            act.setChecked(mode == theme.mode)
        self._refresh_qemu_state()
        self._set_op_state(self.runner.busy)
        self._rerender_log()

    def set_theme(self, mode: str) -> None:
        theme.apply(QApplication.instance(), mode)
        self.settings.setValue("theme", mode)

    def toggle_theme(self) -> None:
        self.set_theme("light" if theme.palette.dark else "dark")

    # ------------------------------------------------------------------ logging

    def log(self, message: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log_entries.append((stamp, level, message))
        if len(self._log_entries) > MAX_LOG_ENTRIES:
            del self._log_entries[:len(self._log_entries) - MAX_LOG_ENTRIES]
        self.log_view.appendHtml(self._log_html(stamp, level, message))
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _log_html(self, stamp: str, level: str, message: str) -> str:
        pal = theme.palette
        color = {"info": pal.accent, "success": pal.success, "warning": pal.warning,
                 "error": pal.danger, "detail": pal.text_faint}.get(level, pal.text_muted)
        text_color = pal.text_muted if level == "detail" else pal.text
        glyph = {"success": "✓", "warning": "!", "error": "✕", "detail": "·"}.get(level, "•")
        return (f'<span style="color:{pal.text_faint}">{stamp}</span>&nbsp;&nbsp;'
                f'<span style="color:{color}"><b>{glyph}</b></span>&nbsp;&nbsp;'
                f'<span style="color:{text_color}">{html.escape(message)}</span>')

    def _rerender_log(self) -> None:
        if not hasattr(self, "log_view"):
            return
        self.log_view.clear()
        for entry in self._log_entries[-MAX_LOG_ENTRIES:]:
            self.log_view.appendHtml(self._log_html(*entry))

    def _clear_log(self) -> None:
        self._log_entries.clear()
        self.log_view.clear()

    # ------------------------------------------------------------------ qemu-img

    def _refresh_qemu_state(self, announce: bool = False) -> None:
        pal = theme.palette
        if self.qemu is None:
            self.qemu_dot.set_color(pal.danger)
            self.qemu_text.setText("qemu-img missing")
            self.qemu_pill.setToolTip("qemu-img does the conversion. Click to install it.")
            if announce:
                self.log(f"qemu-img is not installed. Install it with: {system.qemu_install_command()}",
                         "error")
        else:
            missing = self.qemu.missing_drivers()
            ok = not self.qemu.too_old and not missing
            self.qemu_dot.set_color(pal.success if ok else pal.warning)
            self.qemu_text.setText(f"qemu-img {self.qemu.version_text}")
            self.qemu_pill.setToolTip(self.qemu.path)
            if announce:
                self.log(f"Using qemu-img {self.qemu.version_text} ({self.qemu.path}).", "detail")
                if missing:
                    self.log(f"This qemu-img can't handle: {', '.join(missing)}.", "warning")
                if self.qemu.too_old:
                    self.log("This qemu-img is too old (2.10 or newer is needed).", "error")
        self.act_install_qemu.setVisible(self.qemu is None)

    def offer_qemu_install(self) -> None:
        if self.qemu is not None:
            QMessageBox.information(self, APP_NAME, f"qemu-img {self.qemu.version_text} is installed "
                                    f"({self.qemu.path}).")
            return
        dialog = QemuMissingDialog(self)
        if not dialog.exec() or not dialog.install_requested:
            return
        self.log("Installing qemu-img…")
        self._set_op_state(True, "Installing qemu-img…")
        self.progress.setRange(0, 0)
        self.runner.start("Install qemu-img", lambda _p, _c: system.install_qemu(graphical=True),
                          self._qemu_installed, self._on_error)

    def _qemu_installed(self, result) -> None:
        ok, message = result
        self.qemu = QemuImg.find()
        self.log(message, "success" if ok else "error")
        if not ok:
            QMessageBox.warning(self, "qemu-img", message)
        self._refresh_qemu_state()
        self._replan_all()

    # ------------------------------------------------------------------ adding and inspecting

    def add_files(self, paths: list[str] | None = None) -> None:
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(self, "Add disk images", self._last_dir(),
                                                    IMAGE_FILTER)
            if not paths:
                return
        known = {item.key for item in self.model.items}
        added = 0
        for p in paths:
            given = Path(p).expanduser()
            if given.is_dir() and not given.name.lower().endswith(".hdd"):
                self.log(f"{given.name} is a folder; add the image files in it.", "warning")
                continue
            # an OVA with several disks is one item per disk; an OVF stands for its disk files
            for path, member in expand_sources(given):
                try:
                    resolved = path.resolve()
                except OSError:
                    resolved = path
                name = f"{path.name} › {member}" if member else path.name
                if (resolved, member) in known:
                    self.log(f"{name} is already in the list.", "detail")
                    continue
                if resolved.is_dir():
                    self.log(f"{path.name} is a folder; add the image files in it.", "warning")
                    continue
                known.add((resolved, member))
                item = Item(source=resolved, target=Target(F.VDI), member=member)
                self.model.add(item)
                self._inspect(item)
                added += 1
        if paths:
            self.settings.setValue("last_dir", str(Path(paths[-1]).expanduser().parent))
        if added and not self.table.selectionModel().hasSelection():
            self.table.selectRow(len(self.model.items) - added)

    def _inspect(self, item: Item) -> None:
        item.state = INSPECTING
        self.model.refresh(item)
        qemu, parents, force = self.qemu, dict(item.parents), item.force_parent
        source, member, item_id = item.source, item.member, item.id

        def work():
            try:
                info = inspect_image(source, qemu, parents, force, member=member)
            except Exception as exc:  # noqa: BLE001 - shown on the item
                info = exc
            self.inspected.emit(item_id, info)

        self._pool.submit(work)

    def _on_inspected(self, item_id: int, info) -> None:
        item = self.model.by_id(item_id)
        if item is None:
            return
        if isinstance(info, BaseException):
            item.info = None
            item.state = BLOCKED
            item.message = f"Could not read the image: {info}"
            self.log(f"{item.name}: {item.message}", "error")
            self.model.refresh(item)
            self._selection_changed()
            return
        first_time = item.info is None
        item.info = info
        if first_time:
            item.target = self._remembered_target(info)
            self._describe_added(item)
        self._replan(item)
        self._selection_changed()

    def _describe_added(self, item: Item) -> None:
        info = item.info
        if info.format is None:
            self.log(f"{item.name}: {'; '.join(info.errors)}", "error")
            return
        text = f"Added {item.name}: {info.label}, {human_bytes(info.virtual_size)}"
        if info.chain:
            text += f", based on {' → '.join(p.name for p in info.chain)}"
        self.log(text)

    def _remembered_target(self, info) -> Target:
        """The last target chosen for this kind of source: one for VirtualBox's own disks
        (they go to Hyper-V by default) and one for everything else (to VirtualBox)."""
        default = F.default_target_for(info.format)
        key = "target_hyperv" if default is F.VHDX else "target_vbox"
        return target_from_setting(str(self.settings.value(key, default.key)), default)

    def _replan(self, item: Item) -> None:
        try:
            self._replan_item(item)
        except OSError as exc:       # a name the file system refuses (too long, …)
            item.plan = None
            item.state = BLOCKED
            item.message = f"{exc.strerror or exc}: {Path(exc.filename).name}" if exc.filename else str(exc)
            self.model.refresh(item)

    def _replan_item(self, item: Item) -> None:
        if item.info is None or item.state in (WAITING, RUNNING, DONE):
            return
        if item.info.format is None:
            item.plan = None
            item.state = BLOCKED
            item.message = item.info.errors[0] if item.info.errors else "Not a disk image"
            self.model.refresh(item)
            return
        if not item.output_custom or item.output is None:
            taken = {i.output for i in self.model.items if i is not item and i.output is not None}
            folder = self.output_folder
            if folder is None and item.info.device:     # not next to /dev/sdb
                last = Path(self._last_dir())
                folder = last if last.is_dir() and not str(last).startswith("/dev") else Path.home()
            item.output = default_output(item.info, item.target, folder, unique=True, taken=taken)
        elif item.output.suffix.lower() not in item.target.format.extensions:
            item.output = item.output.with_suffix(item.target.format.extension)
        job = Job(source=item.source, target=item.target, output=item.output,
                  overwrite=self.overwrite_chk.isChecked() or item.overwrite, parents=dict(item.parents),
                  force_parent=item.force_parent, member=item.member)
        item.plan = make_plan(job, self.qemu, item.info)
        item.target = job.target
        clash = next((i for i in self.model.items if i is not item and i.output == item.output
                      and i.state != DONE), None)
        if clash is not None:
            item.plan.errors.append(f"{clash.name} is also saved as {item.output.name}. "
                                    "Choose another name.")
        if item.plan.ok:
            item.state = READY
            item.message = ""
        else:
            item.state = BLOCKED
            item.message = _short(item.plan.errors[0])
        self.model.refresh(item)

    def _replan_all(self) -> None:
        for item in self.model.items:
            if item.info is not None and item.state not in (WAITING, RUNNING, DONE, INSPECTING):
                self._replan(item)
        self._selection_changed()

    # ------------------------------------------------------------------ selection and editing

    def selected_items(self) -> list[Item]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.items[r] for r in rows if r < len(self.model.items)]

    def current_item(self) -> Item | None:
        items = self.selected_items()
        return items[0] if items else None

    def _queue_changed(self) -> None:
        items = self.model.items
        self.queue_stack.setCurrentIndex(1 if items else 0)
        if items:
            total = sum(i.info.virtual_size for i in items if i.info is not None)
            done = sum(1 for i in items if i.state == DONE)
            text = f"· {len(items)} image{'s' if len(items) != 1 else ''}, {human_bytes(total)}"
            if done:
                text += f" · {done} done"
            self.queue_count.setText(text)
        else:
            self.queue_count.setText("")
        self._update_actions()

    def _selection_changed(self) -> None:
        item = self.current_item()
        self._show_image(item)
        self._show_target(item)
        self._update_actions()
        self._queue_changed()

    def _show_image(self, item: Item | None) -> None:
        info = item.info if item is not None else None
        self.img_empty.setVisible(item is None)
        self.img_body.setVisible(item is not None)
        self.img_badge.setVisible(item is not None)
        if item is None:
            return
        self.img_name.setText(breakable(info.name if info is not None else item.name))
        if info is None:
            self.img_sub.setText("Reading…" if item.state == INSPECTING else item.message)
            set_badge(self.img_badge, "READING" if item.state == INSPECTING else "ERROR",
                      "Idle" if item.state == INSPECTING else "Bad")
            for t in (self.tile_size, self.tile_file, self.tile_format):
                t.set("—")
            for key in ("parts", "boot", "chain", "creator"):
                self.img_grid.set(key, "—")
            self.img_banner.hide()
            self.parent_btn.hide()
            self.repair_btn.hide()
            self.write_btn.hide()
            return
        fmt = info.format
        self.img_sub.setText(info.label if fmt else "Not a disk image")
        tip = [str(item.source)]
        if info.disk_id:
            tip.append(f"Disk ID {info.disk_id}")
        self.img_name.setToolTip("\n".join(tip))
        self.img_sub.setToolTip("\n".join(tip))
        if not info.ok:
            set_badge(self.img_badge, "PROBLEM", "Bad")
        elif info.device:
            set_badge(self.img_badge, "IN USE" if info.mountpoints else "DISK", "Warn" if info.mountpoints else "Info")
        elif info.differencing:
            set_badge(self.img_badge, "CHECKPOINT", "Info")
        else:
            set_badge(self.img_badge, "OK", "Ok")
        self.tile_size.set(human_bytes(info.virtual_size) if info.virtual_size else "—",
                           f"{info.virtual_size:,} bytes")
        self.tile_file.set(human_bytes(info.allocated) if fmt else "—",
                           f"{info.file_size:,} bytes, {info.allocated:,} on disk")
        block = f", {human_bytes(info.block_size)} blocks" if info.block_size else ""
        self.tile_format.set("Disk" if info.device else fmt.label if fmt else "?", f"{info.subformat}{block}")
        parts = info.partitions
        self.img_grid.set("parts", parts.label if parts else "—", parts.summary() if parts else "")
        self.img_grid.set("boot", parts.boot_label if parts and parts.firmware else "—")
        if info.chain:
            chain = " → ".join(p.name for p in info.chain)
            self.img_grid.set("chain", chain, "\n".join(str(p.path) for p in info.chain))
        elif info.differencing:
            self.img_grid.set("chain", "parent not found")
        elif info.backing_file:
            self.img_grid.set("chain", Path(info.backing_file.replace("\\", "/")).name, info.backing_file)
        else:
            self.img_grid.set("chain", "— (a complete disk)")
        self.img_grid.set("creator", info.creator or "—")
        problems = info.errors
        if problems:
            self.img_banner.setText("<br>".join(f"✕ {html.escape(p)}" for p in problems))
            self.img_banner.show()
        else:
            self.img_banner.hide()
        # (a disk inside an OVA can't be repaired or given a parent: it is unpacked first)
        self.parent_btn.setVisible(info.differencing and (not info.chain or bool(problems))
                                   and item.editable and not info.member)
        self.repair_btn.setVisible(info.dirty and item.editable and not info.member)
        self.write_btn.setVisible(info.ok and fmt is not None and not fmt.container)

    def _show_target(self, item: Item | None) -> None:
        self._updating_controls = True
        try:
            enabled = item is not None and item.editable and item.info is not None \
                and item.info.format is not None
            for w in (self.picker, self.out_edit, self.out_btn):
                w.setEnabled(enabled)
            self._shown_target = item.target if item is not None else Target(F.VDI)
            self.picker.set_target(self._shown_target)
            self.out_edit.setText(str(item.output) if item is not None and item.output else "")
            self.out_edit.setCursorPosition(len(self.out_edit.text()))
            self._show_plan(item)
        finally:
            self._updating_controls = False

    def _show_plan(self, item: Item | None) -> None:
        plan = item.plan if item is not None else None
        lines = []
        pal = theme.palette
        if item is not None and item.state == DONE and item.outcome is not None:
            o = item.outcome
            lines.append(f'<span style="color:{pal.success}">✓</span> Saved {html.escape(o.output.name)} '
                         f"({human_bytes(o.file_size)}"
                         + (", identical to the source" if o.verified else "") + ")")
        elif item is not None and item.state == FAILED and item.message:
            lines.append(f'<span style="color:{pal.danger}">✕</span> {html.escape(item.message)}')
        if plan is not None and item.state != DONE:
            info_errors = set(item.info.errors) if item.info else set()
            for e in plan.errors:
                if e not in info_errors:
                    lines.append(f'<span style="color:{pal.danger}">✕</span> {html.escape(e)}')
            for w in plan.warnings:
                lines.append(f'<span style="color:{pal.warning}">!</span> {html.escape(w)}')
            for n in plan.notes:
                lines.append(f'<span style="color:{pal.accent}">•</span> {html.escape(n)}')
        self.plan_banner.setText("<br>".join(lines))
        self.plan_banner.setVisible(bool(lines))

    def _target_edited(self) -> None:
        """Apply what was changed in the picker (only that: with several images selected, each keeps
        its other choices) to the selected images; ones it doesn't change stay as they are."""
        if self._updating_controls:
            return
        new, old = self.picker.target(), self._shown_target
        if new == old:
            return              # the format that was already chosen, clicked again
        changed = {name for name in ("format", "fixed", "stream", "azure", "split")
                   if getattr(new, name) != getattr(old, name)}
        for item in self.selected_items():
            if not item.editable or item.info is None or item.info.format is None:
                continue
            if "format" in changed:
                target = new
            else:
                fields = {name: getattr(new if name in changed else item.target, name)
                          for name in ("fixed", "stream", "azure", "split")}
                target = Target(item.target.format, **fields).normalized()
            if target == item.target:
                continue
            item.target = target
            if item.state == DONE:
                item.state = READY
                item.outcome = None
            self._replan(item)
            default = F.default_target_for(item.info.format)
            parts = ["azure"] if target.azure else ["stream"] if target.stream else ["fixed"] if target.fixed else []
            variant = "+".join(parts + (["split"] if target.split else []))
            self.settings.setValue("target_hyperv" if default is F.VHDX else "target_vbox",
                                   target.format.key + (f":{variant}" if variant else ""))
        self._show_target(self.current_item())
        self._update_actions()

    def _output_typed(self) -> None:
        item = self.current_item()
        text = self.out_edit.text().strip()
        if item is None or not item.editable or not text or self._updating_controls:
            return
        path = Path(os.path.expanduser(text))
        if not path.is_absolute():
            path = item.source.parent / path
        if path != item.output:
            item.output = path
            item.output_custom = True
            item.overwrite = False
            self._replan(item)
            self._show_target(item)

    def choose_output(self) -> None:
        item = self.current_item()
        if item is None or not item.editable:
            return
        fmt = item.target.format
        start = str(item.output) if item.output else str(item.source.parent)
        path, _ = QFileDialog.getSaveFileName(self, "Save the converted disk as", start,
                                              f"{fmt.label} (*{fmt.extension});;All files (*)")
        if not path:
            return
        chosen = Path(path)
        out = chosen if chosen.suffix else chosen.with_suffix(fmt.extension)
        item.output = out
        item.output_custom = True
        # the save dialog asked whether to replace the file it returned (not one with an added
        # extension): for this item only
        item.overwrite = out == chosen and chosen.exists()
        self._replan(item)
        self._show_target(item)
        self._update_actions()

    def _folder_choice(self, index: int) -> None:
        if self.folder_box.itemData(index) == "choose":
            folder = QFileDialog.getExistingDirectory(self, "Save converted disks in",
                                                      str(self.output_folder or self._last_dir()))
            if folder:
                self._set_output_folder(Path(folder))
            else:
                self._set_output_folder(self.output_folder)
        elif index == 0:
            self._set_output_folder(None)
        else:
            self._set_output_folder(Path(self.folder_box.itemData(index)))

    def _set_output_folder(self, folder: Path | None) -> None:
        self.output_folder = folder
        while self.folder_box.count() > 2:
            self.folder_box.removeItem(2)
        if folder is not None:
            self.folder_box.addItem(folder.name or str(folder), str(folder))
            self.folder_box.setItemData(2, str(folder), Qt.ToolTipRole)
            self.folder_box.setCurrentIndex(2)
        else:
            self.folder_box.setCurrentIndex(0)
        self.settings.setValue("output_folder", str(folder) if folder else "")
        for item in self.model.items:
            if not item.output_custom and item.state not in (RUNNING, WAITING, DONE):
                item.output = None
        self._replan_all()

    # ------------------------------------------------------------------ actions on items

    def _update_actions(self) -> None:
        busy = self.runner.busy
        items = self.model.items
        selected = self.selected_items()
        startable = [i for i in items if i.state in STARTABLE and i.plan is not None and i.plan.ok]
        self.act_convert.setEnabled(not busy and bool(startable) and self.qemu is not None)
        self.act_remove.setEnabled(any(i.state not in (RUNNING, WAITING) for i in selected))
        self.act_clear.setEnabled(any(i.state == DONE for i in items))
        self.act_folder.setEnabled(any(i.output is not None for i in selected))
        self.act_copy_path.setEnabled(any(i.output is not None for i in selected))
        self.act_verify.setEnabled(not busy and self.qemu is not None)
        cur = self.current_item()
        info = cur.info if cur is not None else None
        self.act_parent.setEnabled(bool(info and info.differencing and cur.editable and not info.member))
        self.act_repair.setEnabled(bool(info and info.dirty and cur.editable and not info.member) and not busy)
        self.act_cancel.setEnabled(busy)
        self.act_add.setEnabled(True)
        self.act_write.setEnabled(not busy and self.qemu is not None)
        self.write_btn.setEnabled(not busy and self.qemu is not None)

    def remove_selected(self) -> None:
        items = [i for i in self.selected_items() if i.state not in (RUNNING, WAITING)]
        self.model.remove(items)
        self._selection_changed()

    def clear_finished(self) -> None:
        self.model.remove([i for i in self.model.items if i.state == DONE])
        self._selection_changed()

    def show_output(self) -> None:
        item = self.current_item()
        if item is None:
            return
        path = item.output if item.output is not None and item.output.exists() else item.source
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def copy_output_path(self) -> None:
        item = self.current_item()
        if item is not None and item.output is not None:
            QApplication.clipboard().setText(str(item.output))
            self.toast.show_message("Path copied", "info", 1800)

    def choose_parent(self) -> None:
        item = self.current_item()
        if item is None or item.info is None or not item.info.differencing or not item.editable:
            return
        # the image whose parent is missing: the last one found in the chain
        child = item.info.chain[-1] if item.info.chain and item.info.chain[-1].differencing else item.info
        exts = " ".join(f"*{e}" for e in child.format.extensions)
        path, _ = QFileDialog.getOpenFileName(self, f"Choose the parent of {child.name}",
                                              str(child.path.parent),
                                              f"{child.format.label} images ({exts});;All files (*)")
        if not path:
            return
        item.parents[child.path] = Path(path)
        item.force_parent = False
        self.log(f"Using {Path(path).name} as the parent of {child.name}.")
        self._inspect(item)
        QTimer.singleShot(400, lambda: self._offer_force_parent(item, child.path))

    def _offer_force_parent(self, item: Item, child: Path) -> None:
        if item.state == INSPECTING:
            QTimer.singleShot(300, lambda: self._offer_force_parent(item, child))
            return
        info = item.info
        if info is None or item.force_parent or not any("doesn't match" in e for e in info.errors):
            return
        if ask_yes_no(self, "Parent doesn't match",
                      "This file is not the parent the checkpoint was made from: its ID is different.",
                      yes="Use it anyway", danger=True,
                      informative="That happens when the parent was changed or copied after the "
                                  "checkpoint was made. The merged disk is likely to be damaged. Only "
                                  "continue if you know the files belong together."):
            item.force_parent = True
            self.log(f"Using a parent with a different ID for {item.name}, as asked.", "warning")
            self._inspect(item)

    def repair_selected(self) -> None:
        item = self.current_item()
        if item is None or item.info is None or not item.info.dirty or self.qemu is None:
            return
        if not ask_yes_no(self, "Repair the VHDX",
                          f"{item.name} wasn't closed cleanly: its log still holds changes.",
                          yes="Repair", danger=True,
                          informative="Repairing applies the log to the file, as Hyper-V would when "
                                      "it opens it. This changes the file; make a copy first if you "
                                      "can. Shut the virtual machine down before repairing."):
            return
        info, qemu = item.info, self.qemu
        self.log(f"Repairing {item.name}…")
        self.runner.start("Repair", lambda _p, _c: repair(info, qemu),
                          lambda msg: self._repaired(item, msg), self._on_error)
        self._set_op_state(True, "Repairing…")
        self.progress.setRange(0, 0)

    def _repaired(self, item: Item, message: str) -> None:
        self.log(f"{item.name}: {message.splitlines()[-1] if message else 'repaired'}", "success")
        self._inspect(item)

    def compare_files(self) -> None:
        if self.qemu is None:
            self.offer_qemu_install()
            return
        first, _ = QFileDialog.getOpenFileName(self, "Compare: the first image", self._last_dir(),
                                               IMAGE_FILTER)
        if not first:
            return
        second, _ = QFileDialog.getOpenFileName(self, "Compare: the second image",
                                                str(Path(first).parent), IMAGE_FILTER)
        if not second:
            return
        qemu = self.qemu
        names = f"{Path(first).name} and {Path(second).name}"

        def job(progress, cancel):
            a = inspect_image(first, qemu)
            b = inspect_image(second, qemu)
            for info in (a, b):
                if not info.ok:
                    raise ConvertError("; ".join(info.errors))
            return verify_images(a, b, qemu, progress, cancel)

        self.log(f"Comparing {names}…")
        self._start("Compare", job, lambda r: self._compared(names, r))

    def _compared(self, names: str, result) -> None:
        same, detail = result
        if same:
            self.log(f"{names} contain the same disk.", "success")
            self.toast.show_message(f"Identical: {names}", "success")
        else:
            self.log(f"{names} differ: {detail}", "error")
            QMessageBox.warning(self, "Compare", f"{names} are different.\n\n{detail}")

    def _table_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if index.isValid() and not self.table.selectionModel().isRowSelected(index.row(), index.parent()):
            self.table.selectRow(index.row())
        menu = QMenu(self)
        for act in (self.act_convert, None, self.act_folder, self.act_copy_path, None,
                    self.act_write, self.act_parent, self.act_repair, None, self.act_remove, self.act_clear):
            if act is None:
                menu.addSeparator()
            elif act.isEnabled():
                menu.addAction(act)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ converting

    def convert_all(self) -> None:
        if self.qemu is None:
            self.offer_qemu_install()
            return
        if self.runner.busy:
            return
        self._output_typed()   # a name typed into "Save as" without pressing Enter
        candidates = [i for i in self.model.items if i.state in STARTABLE and i.plan and i.plan.ok]
        warned = [i for i in candidates if i.plan.warnings]
        if warned:
            text = "\n".join(f"• {i.name}: {i.plan.warnings[0]}" for i in warned[:4])
            if not ask_yes_no(self, "Convert", "Some images have warnings:", yes="Convert anyway",
                              informative=text):
                return
        if not candidates:
            return
        for item in candidates:
            item.state = WAITING
            item.message = ""
            self.model.refresh(item)
        self._stop_queue = False
        self.log(f"Converting {len(candidates)} image{'s' if len(candidates) > 1 else ''}.")
        self._next_in_queue()

    def _next_in_queue(self) -> None:
        if self._stop_queue:
            for item in self.model.items:
                if item.state == WAITING:
                    item.state = READY
                    self.model.refresh(item)
            self._stop_queue = False
            self._update_actions()
            return
        item = next((i for i in self.model.items if i.state == WAITING), None)
        if item is None:
            self._update_actions()
            return
        self._run_item(item)

    def _run_item(self, item: Item) -> None:
        qemu = self.qemu
        job = Job(source=item.source, target=item.target, output=item.output,
                  verify=self.verify_chk.isChecked(),
                  overwrite=self.overwrite_chk.isChecked() or item.overwrite,
                  parents=dict(item.parents), force_parent=item.force_parent, member=item.member,
                  elevate="pkexec")
        log = self.logLine.emit

        def work(progress, cancel):
            plan = make_plan(job, qemu)   # look at the files again: they may have changed
            if not plan.ok:
                raise ConvertError(plan.errors[0])
            return execute(plan, qemu, progress, cancel, log=lambda m: log(m, "detail"))

        item.state = RUNNING
        item.stage, item.done, item.total = "Converting", 0, item.info.virtual_size if item.info else 0
        self._running = item
        self.model.refresh(item)
        self.log(f"{item.name} → {item.output.name} ({item.target.label})")
        self._start(f"Converting {item.name}", work, lambda outcome: self._item_done(item, outcome),
                    on_error=lambda exc: self._item_failed(item, exc))

    def _item_done(self, item: Item, outcome) -> None:
        item.state = DONE
        item.outcome = outcome
        item.done = item.total
        self.model.refresh(item)
        check = ", identical to the source" if outcome.verified else ""
        files = f" in {len(outcome.files)} files" if len(outcome.files) > 1 else ""
        self.log(f"Saved {outcome.output} ({human_bytes(outcome.file_size)}{files}{check}, "
                 f"{mmss(outcome.seconds)}).", "success")
        self.toast.show_message(f"{outcome.output.name} is ready" + (" and verified" if outcome.verified else ""),
                                "success")
        if item is self.current_item():
            self._selection_changed()

    def _item_failed(self, item: Item, exc: BaseException) -> None:
        if isinstance(exc, OperationCancelled):
            item.state = CANCELLED
            item.message = ""
            self.log(f"{item.name}: cancelled; the unfinished file was removed.", "warning")
            self.toast.show_message("Cancelled", "warning")
            self._stop_queue = True
        else:
            item.state = FAILED
            item.message = (str(exc) if isinstance(exc, ConvertError) else
                            os_error_text(exc) if isinstance(exc, OSError) else repr(exc))
            level = "error"
            self.log(f"{item.name}: {item.message}", level)
            if isinstance(exc, VerifyError):
                QMessageBox.critical(self, "The copy is different", f"{item.name}: {exc}\n\nThe "
                                     "converted file was deleted. Please report this with the log.")
            elif not isinstance(exc, (ConvertError, OSError)):
                detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                box = QMessageBox(QMessageBox.Critical, "Unexpected error", str(exc) or repr(exc),
                                  parent=self)
                box.setDetailedText(detail)
                box.exec()
        self.model.refresh(item)
        if item is self.current_item():
            self._selection_changed()

    def _start(self, title: str, job, on_success, on_error=None) -> None:
        if self.runner.busy:
            return
        self._stage = ""
        self.runner.start(title, job, on_success, on_error or self._on_error)
        self._set_op_state(True, f"{title}…")

    def _set_op_state(self, busy: bool, text: str | None = None) -> None:
        pal = theme.palette
        if text is not None:
            self.op_label.setText(text)
        self.op_dot.set_color(pal.accent if busy else pal.success, halo=busy)
        self.progress.setVisible(busy)
        self.cancel_btn.setVisible(busy)
        self.act_cancel.setEnabled(busy)
        if not busy:
            self.op_detail.setText("")
        self._update_actions()

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        if stage != self._stage:
            self._stage = stage
            self._stage_t0 = now
        item = self._running
        name = f" {item.name}" if item is not None else ""
        self.op_label.setText(f"{stage}{name}…")
        elapsed = now - self._stage_t0
        if total > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(1000 * done / total))
            rate = done / elapsed if elapsed > 0.5 else 0
            eta = (total - done) / rate if rate > 0 else 0
            detail = f"{100 * done / total:5.1f}%"
            if rate > 0:
                detail += f"   {human_bytes(rate)}/s   ETA {mmss(eta)}"
            self.op_detail.setText(detail)
        else:
            self.progress.setRange(0, 0)
            self.op_detail.setText(f"{mmss(elapsed)} elapsed")
        if item is not None and item.state == RUNNING:
            item.stage, item.done, item.total = stage, done, total
            self.model.refresh(item)

    def _on_finished(self, title: str) -> None:
        elapsed = time.monotonic() - self.runner.started_at
        self.progress.setRange(0, 1000)
        self._running = None
        self._set_op_state(False, "Ready")
        self.last_op.setText(f"Last: {title} · {mmss(elapsed)}")
        self._queue_changed()
        QTimer.singleShot(0, self._next_in_queue)

    def _on_error(self, exc: BaseException) -> None:
        if isinstance(exc, OperationCancelled):
            self.log("Cancelled.", "warning")
            self.toast.show_message("Cancelled", "warning")
            return
        if isinstance(exc, (ConvertError, OSError)):
            text = os_error_text(exc) if isinstance(exc, OSError) else str(exc)
            self.log(text, "error")
            QMessageBox.critical(self, APP_NAME, text)
            return
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.log(f"Unexpected error: {exc!r}", "error")
        box = QMessageBox(QMessageBox.Critical, "Unexpected error", str(exc) or repr(exc), parent=self)
        box.setDetailedText(detail)
        box.exec()

    def cancel_operation(self) -> None:
        if self.runner.busy:
            self._stop_queue = True
            self.runner.cancel()
            self.op_label.setText("Cancelling…")

    # ------------------------------------------------------------------ new disks

    def new_disk(self) -> None:
        if self.qemu is None:
            self.offer_qemu_install()
            return
        if self.runner.busy:
            self.toast.show_message(f"Wait until {self.runner.name} has finished", "warning")
            return
        dialog = CreateDiskDialog(self, self.qemu, self.settings)
        try:
            if not dialog.exec():
                return
            disk, job = dialog.disk, dialog.job
        finally:
            dialog.deleteLater()
        if job is not None:
            self._copy_disk(job)
            return
        if disk is None:
            return
        qemu, log = self.qemu, self.logLine.emit
        plan = make_create_plan(disk, qemu)

        def work(progress, cancel):
            again = make_create_plan(disk, qemu)     # the folder may have changed meanwhile
            if not again.ok:
                raise ConvertError(again.errors[0])
            return create_disk(again, qemu, progress, cancel, log=lambda m: log(m, "detail"))

        self.log(f"Creating {disk.output}: {plan.summary}")
        self._start(f"Creating {disk.output.name}", work, self._disk_created)

    def _copy_disk(self, job: Job) -> None:
        """A virtual disk with the contents of a physical one (chosen in the New disk dialog)."""
        qemu, log = self.qemu, self.logLine.emit

        def work(progress, cancel):
            plan = make_plan(job, qemu)        # look at the disk again: it may have been unplugged
            if not plan.ok:
                raise ConvertError(plan.errors[0])
            return execute(plan, qemu, progress, cancel, log=lambda m: log(m, "detail"))

        self.log(f"Copying {job.source} into {job.output} ({job.target.label})")
        self._start(f"Copying {job.source.name}", work, self._disk_created)

    def add_physical_disk(self) -> None:
        """Pick one of the computer's disks and put it in the list, to convert it like an image."""
        dialog = DiskPickerDialog(self)
        try:
            path = dialog.exec() and dialog.chooser.path()
        finally:
            dialog.deleteLater()
        if path:
            self.add_files([path])

    # ------------------------------------------------------------------ writing onto a physical disk

    def write_to_disk(self) -> None:
        """Put the selected image (or one chosen in the dialog) onto a physical disk."""
        if self.qemu is None:
            self.offer_qemu_install()
            return
        if self.runner.busy:
            self.toast.show_message(f"Wait until {self.runner.name} has finished", "warning")
            return
        item = self.current_item()
        usable = item is not None and item.info is not None and item.info.format is not None
        dialog = WriteDiskDialog(self, self.qemu, self.settings, item.source if usable else None,
                                 item.info if usable else None, item.member if usable else None,
                                 item.parents if usable else None, item.force_parent if usable else False,
                                 image_filter=IMAGE_FILTER)
        try:
            job = dialog.job if dialog.exec() else None
        finally:
            dialog.deleteLater()
        if job is None:
            return
        if self.runner.busy:            # something started while the dialog was open
            QMessageBox.warning(self, "Write to disk", f"{self.runner.name} is running. Nothing was written; "
                                "write the disk when it has finished.")
            return
        qemu, log = self.qemu, self.logLine.emit

        def work(progress, cancel):
            plan = make_write_plan(job, qemu)     # look again: the disk may have been unplugged or mounted
            if not plan.ok:
                raise ConvertError(plan.errors[0])
            return write_to_disk(plan, qemu, progress, cancel, log=lambda m: log(m, "detail"))

        self.log(f"Writing {job.source.name}{f' › {job.member}' if job.member else ''} onto {job.disk}")
        self._start(f"Writing to {job.disk.name}", work, self._disk_written,
                    on_error=lambda exc: self._write_failed(job, exc))

    def _disk_written(self, outcome) -> None:
        check = ", read back and identical" if outcome.verified else ""
        self.log(f"Wrote {human_bytes(outcome.size)} onto {outcome.title}{check} ({mmss(outcome.seconds)}).",
                 "success")
        if outcome.fitted:
            self.log("The partition table was fitted to the disk's size.", "detail")
        self.toast.show_message(f"{outcome.title} is ready" + (" and verified" if outcome.verified else ""),
                                "success")

    def _write_failed(self, job, exc: BaseException) -> None:
        if isinstance(exc, OperationCancelled):
            if getattr(exc, "written", False):
                self.log(f"Stopped: {job.disk} is written, but wasn't compared with the image (or fitted to the "
                         "disk's size). Write it again to be sure.", "warning")
            else:
                self.log(f"Stopped: {job.disk} is only partly written. Write it again, or format it.", "warning")
            self.toast.show_message("Stopped", "warning")
        elif isinstance(exc, VerifyError):
            self.log(str(exc), "error")
            QMessageBox.critical(self, "The disk is different", str(exc))
        else:
            self._on_error(exc)

    def _disk_created(self, outcome) -> None:
        check = ", checked" if outcome.verified else ""
        files = f" in {len(outcome.files)} files" if len(outcome.files) > 1 else ""
        self.log(f"Created {outcome.output} ({human_bytes(outcome.size)} disk, "
                 f"{human_bytes(outcome.file_size)} file{files}{check}).", "success")
        self.toast.show_message(f"{outcome.output.name} is ready", "success")
        self.settings.setValue("create/last_output", str(outcome.output))

    # ------------------------------------------------------------------ misc

    def show_about(self) -> None:
        text = ""
        if self.qemu is not None:
            text = f"<b>qemu-img {self.qemu.version_text}</b> · {self.qemu.path}"
        else:
            text = f"<b>qemu-img is not installed.</b> Install it with: {system.qemu_install_command()}"
        AboutDialog(self, text).exec()

    def _last_dir(self) -> str:
        return str(self.settings.value("last_dir", str(Path.home())))

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_zone.set_active(True)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.drop_zone.set_active(False)

    def dropEvent(self, event) -> None:  # noqa: N802
        self.drop_zone.set_active(False)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_files(paths)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast._place()

    def _restore_settings(self) -> None:
        s = self.settings
        geometry = s.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        sizes = s.value("splitter")
        if sizes:
            try:
                self.splitter.setSizes([int(x) for x in sizes])
            except (TypeError, ValueError):
                pass
        self.verify_chk.setChecked(s.value("verify", True, type=bool))
        self.overwrite_chk.setChecked(s.value("overwrite", False, type=bool))
        folder = str(s.value("output_folder", "") or "")
        if folder and Path(folder).is_dir():
            self._set_output_folder(Path(folder))

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("splitter", self.splitter.sizes())
        s.setValue("verify", self.verify_chk.isChecked())
        s.setValue("overwrite", self.overwrite_chk.isChecked())

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.runner.busy:
            if not ask_yes_no(self, "Quit", f"{self.runner.name} is still running. Stop it and quit?",
                              yes="Stop and quit", danger=True):
                event.ignore()
                return
            self._stop_queue = True
            self.runner.cancel()
            self.runner.wait(30000)
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._save_settings()
        event.accept()


def _short(text: str, limit: int = 90) -> str:
    text = text.split(". ")[0].rstrip(".")
    return text if len(text) <= limit else text[:limit - 1] + "…"

