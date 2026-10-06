"""The list of images to convert: items, the table model and the status column."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from ..core.convert import Outcome, Plan, Target
from ..core.probe import ImageInfo
from ..core.util import human_bytes, mmss
from . import icons
from .theme import theme

INSPECTING = "inspecting"
READY = "ready"
BLOCKED = "blocked"
WAITING = "waiting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
STARTABLE = (READY, FAILED, CANCELLED)

_ids = itertools.count(1)


@dataclass(eq=False)
class Item:
    source: Path
    target: Target
    member: str | None = None    # a disk inside the OVA SOURCE
    output: Path | None = None
    info: ImageInfo | None = None
    plan: Plan | None = None
    output_custom: bool = False
    parents: dict[Path, Path] = field(default_factory=dict)
    force_parent: bool = False
    overwrite: bool = False      # the user agreed to replace this item's output
    state: str = INSPECTING
    stage: str = ""
    done: int = 0
    total: int = 0
    message: str = ""
    outcome: Outcome | None = None
    id: int = field(default_factory=lambda: next(_ids))

    @property
    def name(self) -> str:
        if self.info is not None and self.info.device:
            return self.info.name           # "Samsung SSD 870 EVO (sda)"
        return f"{self.source.name} › {self.member}" if self.member else self.source.name

    @property
    def key(self) -> tuple[Path, str | None]:
        return self.source, self.member

    @property
    def from_label(self) -> str:
        if self.info is None:
            return "…"
        if self.info.format is None:
            return "Unknown"
        if self.info.chain:
            return f"{self.info.format.label} checkpoint"
        if self.info.member:
            return f"{self.info.format.label} in OVA"
        if self.info.device:
            return self.info.subformat[:1].upper() + self.info.subformat[1:]     # "Physical disk, USB"
        return self.info.label.replace(" · ", " ")

    @property
    def editable(self) -> bool:
        return self.state not in (INSPECTING, WAITING, RUNNING)

    def status_text(self) -> str:
        if self.state == INSPECTING:
            return "Reading…"
        if self.state == READY:
            warnings = len(self.plan.warnings) if self.plan else 0
            return f"Ready · {warnings} warning{'s' if warnings > 1 else ''}" if warnings else "Ready"
        if self.state == BLOCKED:
            return self.message or "Can't convert"
        if self.state == WAITING:
            return "Waiting"
        if self.state == RUNNING:
            pct = 100 * self.done / self.total if self.total else 0
            return f"{self.stage or 'Converting'} {pct:.0f}%"
        if self.state == DONE and self.outcome is not None:
            verified = " · verified" if self.outcome.verified else ""
            if len(self.outcome.files) > 1:
                verified += f" · {len(self.outcome.files)} files"
            return f"Done{verified} · {mmss(self.outcome.seconds)}"
        if self.state == FAILED:
            return f"Failed: {self.message}" if self.message else "Failed"
        if self.state == CANCELLED:
            return "Cancelled"
        return self.state.capitalize()

    def tooltip(self) -> str:
        lines = [str(self.source)]
        if self.output is not None:
            lines.append(f"→ {self.output}")
        if self.plan is not None:
            lines += [f"✕ {e}" for e in self.plan.errors]
            lines += [f"! {w}" for w in self.plan.warnings]
        if self.state == FAILED and self.message:
            lines.append(f"✕ {self.message}")
        return "\n".join(lines)


COLUMNS = ("Image", "From", "To", "Disk size", "Status")


class QueueModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[Item] = []
        theme.changed.connect(lambda _p: self._refresh_all())

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return item.name
            if col == 1:
                return item.from_label
            if col == 2:
                return item.target.label
            if col == 3:
                return human_bytes(item.info.virtual_size) if item.info and item.info.virtual_size else "—"
            if col == 4:
                return item.status_text()
        if role == Qt.ToolTipRole:
            return item.tooltip()
        if role == Qt.DecorationRole and col == 0:
            name, token = STATE_ICONS.get(item.state, ("file_image", "text_muted"))
            return icons.icon(name, getattr(theme.palette, token))
        if role == Qt.UserRole:
            return item
        return None

    def add(self, item: Item) -> None:
        row = len(self.items)
        self.beginInsertRows(QModelIndex(), row, row)
        self.items.append(item)
        self.endInsertRows()

    def remove(self, items: list[Item]) -> None:
        for item in items:
            if item in self.items:
                row = self.items.index(item)
                self.beginRemoveRows(QModelIndex(), row, row)
                self.items.pop(row)
                self.endRemoveRows()

    def row_of(self, item: Item) -> int:
        return self.items.index(item) if item in self.items else -1

    def by_id(self, item_id: int) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)

    def refresh(self, item: Item) -> None:
        row = self.row_of(item)
        if row >= 0:
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def _refresh_all(self) -> None:
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(COLUMNS) - 1))


STATE_ICONS = {
    INSPECTING: ("clock", "text_faint"),
    READY: ("file_image", "text_muted"),
    BLOCKED: ("error", "danger"),
    WAITING: ("clock", "text_muted"),
    RUNNING: ("convert", "accent"),
    DONE: ("check", "success"),
    FAILED: ("error", "danger"),
    CANCELLED: ("cancel", "warning"),
}


class StatusDelegate(QStyledItemDelegate):
    """Draws the status column: a progress bar while converting, coloured text otherwise."""

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        item: Item = index.data(Qt.UserRole)
        if item is None:
            return super().paint(painter, option, index)
        pal = theme.palette
        painter.save()
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(pal.accent_soft) if not pal.accent_soft.startswith("rgba")
                             else _rgba(pal.accent_soft))
        rect = option.rect.adjusted(8, 0, -10, 0)
        text = item.status_text()
        color = {READY: pal.text, BLOCKED: pal.danger, FAILED: pal.danger, DONE: pal.success,
                 CANCELLED: pal.warning, RUNNING: pal.text}.get(item.state, pal.text_muted)
        if item.state == READY and item.plan is not None and item.plan.warnings:
            color = pal.warning
        if item.state == RUNNING:
            frac = item.done / item.total if item.total else 0.0
            bar_h = 6
            fm = QFontMetrics(option.font)
            label_w = max(fm.horizontalAdvance("Converting 100%"), fm.horizontalAdvance("Verifying 100%")) + 14
            bar = QRectF(rect.left(), rect.center().y() - bar_h / 2,
                         max(40, rect.width() - label_w), bar_h)
            path = QPainterPath()
            path.addRoundedRect(bar, 3, 3)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.fillPath(path, QColor(pal.raised))
            if frac > 0:
                done = QPainterPath()
                done.addRoundedRect(QRectF(bar.left(), bar.top(), max(bar_h, bar.width() * frac), bar_h), 3, 3)
                painter.fillPath(done, QColor(pal.accent))
            painter.setPen(QColor(color))
            text_rect = rect.adjusted(int(bar.width()) + 12, 0, 0, 0)
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        else:
            painter.setPen(QColor(color))
            elided = QFontMetrics(option.font).elidedText(text, Qt.ElideRight, rect.width())
            painter.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        size = super().sizeHint(option, index)
        return QSize(max(size.width(), 220), size.height())


def _rgba(text: str) -> QColor:
    parts = [p.strip() for p in text[text.index("(") + 1:text.index(")")].split(",")]
    r, g, b = (int(x) for x in parts[:3])
    a = float(parts[3]) if len(parts) > 3 else 1.0
    return QColor(r, g, b, int(a * 255))
