"""Small reusable widgets: cards, key/value grids, toasts, badges, the drop zone.

Card, KeyValueGrid, StatTile, StatusDot, Toast and SegmentedControl are shared
with the EZP2019Linux suite.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QButtonGroup,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .theme import theme


def form_row(form: QFormLayout, text: str, field: QWidget, muted: bool = True) -> QLabel:
    """Add a form row whose label is vertically centred on the field."""
    label = QLabel(text)
    if muted:
        label.setObjectName("Muted")
    label.setMinimumHeight(field.sizeHint().height())
    label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    form.addRow(label, field)
    return label


def scaled_font(widget: QWidget, factor: float, bold: bool = False):
    font = widget.font()
    font.setPointSizeF(max(6.0, font.pointSizeF() * factor))
    if bold:
        font.setBold(True)
    return font


class Card(QFrame):
    """A rounded surface with an optional uppercase title and trailing widgets."""

    def __init__(self, title: str | None = None, icon: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 14)
        self._layout.setSpacing(9)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self._icon_name = icon
        self.icon_label = QLabel()
        self.title_label = QLabel((title or "").upper())
        self.title_label.setObjectName("CardTitle")
        self.title_label.setFont(scaled_font(self.title_label, 0.82, bold=True))
        if title:
            if icon:
                self.header.addWidget(self.icon_label)
            self.header.addWidget(self.title_label)
            self.header.addStretch(1)
            self._layout.addLayout(self.header)
        theme.changed.connect(self._retheme)
        self._retheme(theme.palette)

    def _retheme(self, palette) -> None:
        if self._icon_name:
            self.icon_label.setPixmap(icons.pixmap(self._icon_name, palette.text_muted, 16))

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)


class KeyValueGrid(QWidget):
    """Two-column label/value list used for chip and programmer details."""

    def __init__(self, columns: int = 1, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(6)
        self.columns = columns
        self._values: dict[str, QLabel] = {}
        self._count = 0

    def add_row(self, key: str, label: str, value: str = "—") -> QLabel:
        row, col = divmod(self._count, self.columns)
        k = QLabel(label)
        k.setObjectName("Muted")
        v = QLabel(value)
        v.setObjectName("Value")
        v.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.setWordWrap(True)
        self.grid.addWidget(k, row, col * 2)
        self.grid.addWidget(v, row, col * 2 + 1)
        self.grid.setColumnStretch(col * 2 + 1, 1)
        self._values[key] = v
        self._count += 1
        return v

    def set(self, key: str, value: str, tooltip: str = "") -> None:
        label = self._values[key]
        label.setText(value)
        label.setToolTip(tooltip)


class StatTile(QFrame):
    """A compact value-over-caption tile."""

    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Inset")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(1)
        self.value = QLabel("—")
        self.value.setObjectName("Value")
        self.value.setFont(scaled_font(self.value, 1.12, bold=True))
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.caption = QLabel(caption)
        self.caption.setObjectName("Muted")
        self.caption.setFont(scaled_font(self.caption, 0.85))
        lay.addWidget(self.value)
        lay.addWidget(self.caption)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set(self, value: str, tooltip: str = "") -> None:
        self.value.setText(value)
        self.setToolTip(tooltip)


class StatusDot(QWidget):
    """A small filled circle with an optional soft halo."""

    def __init__(self, size: int = 10, parent=None):
        super().__init__(parent)
        self._color = QColor("#888")
        self._halo = True
        self.setFixedSize(QSize(size + 8, size + 8))

    def set_color(self, color: str, halo: bool = True) -> None:
        self._color = QColor(color)
        self._halo = halo
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        if self._halo:
            halo = QColor(self._color)
            halo.setAlpha(55)
            p.setBrush(halo)
            p.setPen(Qt.NoPen)
            p.drawEllipse(r)
        inner = r.adjusted(4, 4, -4, -4)
        p.setBrush(self._color)
        p.setPen(Qt.NoPen)
        p.drawEllipse(inner)


class Toast(QWidget):
    """A transient notification floating near the bottom right of its parent."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self._icon = QLabel()
        self._text = QLabel()
        self._text.setWordWrap(True)
        self._text.setMaximumWidth(420)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 16, 10)
        lay.setSpacing(10)
        lay.addWidget(self._icon, 0, Qt.AlignTop)
        lay.addWidget(self._text, 1)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self._fade_out)
        self._accent = QColor("#5b8cff")
        self.hide()

    def show_message(self, text: str, kind: str = "success", timeout_ms: int = 3500) -> None:
        pal = theme.palette
        color = {"success": pal.success, "warning": pal.warning, "error": pal.danger,
                 "info": pal.accent}.get(kind, pal.accent)
        name = {"success": "check", "warning": "warning", "error": "error"}.get(kind, "info")
        self._accent = QColor(color)
        self._icon.setPixmap(icons.pixmap(name, color, 18))
        self._text.setText(text)
        self.adjustSize()
        self._place()
        self.raise_()
        self.show()
        self._anim.stop()
        self._anim.setDuration(180)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(timeout_ms)

    def _fade_out(self) -> None:
        self._anim.stop()
        self._anim.setDuration(400)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self._hide_once)
        self._anim.start()

    def _hide_once(self) -> None:
        self._anim.finished.disconnect(self._hide_once)
        self.hide()

    def _place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        size = self.sizeHint()
        x = parent.width() - size.width() - 22
        y = parent.height() - size.height() - 56
        self.setGeometry(QRect(x, y, size.width(), size.height()))

    def mousePressEvent(self, _event) -> None:  # noqa: N802
        self._timer.stop()
        self._fade_out()

    def paintEvent(self, _event) -> None:  # noqa: N802
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.fillPath(path, QColor(pal.raised))
        p.setPen(QColor(pal.border_strong))
        p.drawPath(path)
        bar = QPainterPath()
        bar.addRoundedRect(rect.adjusted(0, 0, -(rect.width() - 4), 0), 2, 2)
        p.fillPath(bar, self._accent)


class SegmentedControl(QFrame):
    """A row (or, with COLUMNS, a grid) of mutually exclusive toggle buttons."""

    changed = Signal(object)

    def __init__(self, parent=None, columns: int = 0):
        super().__init__(parent)
        self.setObjectName("Segmented")
        self._columns = columns
        self._layout = QGridLayout(self) if columns else QHBoxLayout(self)
        self._layout.setContentsMargins(3, 3, 3, 3)
        self._layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._values: list[object] = []
        self._group.idClicked.connect(lambda i: self.changed.emit(self._values[i]))

    def add(self, label: str, value: object, tooltip: str = "") -> None:
        btn = QToolButton()
        btn.setObjectName("Segment")
        btn.setText(label)
        btn.setToolTip(tooltip)
        btn.setCheckable(True)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn.setCursor(Qt.PointingHandCursor)
        index = len(self._values)
        self._group.addButton(btn, index)
        self._values.append(value)
        if self._columns:
            self._layout.addWidget(btn, index // self._columns, index % self._columns)
        else:
            self._layout.addWidget(btn)

    def clear(self) -> None:
        for btn in self._group.buttons():
            self._group.removeButton(btn)
            btn.deleteLater()
        self._values = []

    def value(self) -> object:
        i = self._group.checkedId()
        return self._values[i] if i >= 0 else None

    def set_value(self, value: object) -> None:
        if value in self._values:
            self._group.button(self._values.index(value)).setChecked(True)

    def set_item_enabled(self, value: object, enabled: bool, tooltip: str | None = None) -> None:
        if value in self._values:
            btn = self._group.button(self._values.index(value))
            btn.setEnabled(enabled)
            if tooltip is not None:
                btn.setToolTip(tooltip)


def badge(text: str, kind: str = "Idle") -> QLabel:
    """A small outlined label: kind is Ok, Idle, Info, Warn or Bad."""
    label = QLabel(text)
    label.setObjectName(f"Badge{kind}")
    label.setFont(scaled_font(label, 0.86, bold=True))
    label.setAlignment(Qt.AlignCenter)
    return label


def set_badge(label: QLabel, text: str, kind: str) -> None:
    label.setText(text)
    if label.objectName() != f"Badge{kind}":
        label.setObjectName(f"Badge{kind}")
        label.style().unpolish(label)
        label.style().polish(label)


class DropZone(QFrame):
    """The empty state of the queue: a dashed area to drop images on, with an Add button
    (and, given SECONDARY, a second one next to it)."""

    addRequested = Signal()
    secondaryRequested = Signal()

    def __init__(self, parent=None, text: str = "", button: str = "Add images…", secondary: str = ""):
        super().__init__(parent)
        self.setObjectName("DropZone")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(10)
        lay.addStretch(1)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignCenter)
        title = QLabel("Drop disk images here")
        title.setObjectName("Heading")
        title.setFont(scaled_font(title, 1.3, bold=True))
        title.setAlignment(Qt.AlignCenter)
        text = QLabel(text)
        text.setObjectName("Muted")
        text.setAlignment(Qt.AlignCenter)
        self.button = QPushButton(button)
        self.button.setProperty("variant", "primary")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(self.addRequested)
        lay.addWidget(self.icon)
        lay.addWidget(title)
        lay.addWidget(text, 0, Qt.AlignHCenter)
        lay.addSpacing(6)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        buttons.addWidget(self.button)
        self.secondary = None
        if secondary:
            self.secondary = QPushButton(secondary)
            self.secondary.setCursor(Qt.PointingHandCursor)
            self.secondary.clicked.connect(self.secondaryRequested)
            buttons.addWidget(self.secondary)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        lay.addStretch(1)
        theme.changed.connect(self._retheme)
        self._retheme(theme.palette)

    def _retheme(self, palette) -> None:
        self.icon.setPixmap(icons.pixmap("drop", palette.text_faint, 44))

    def set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)
