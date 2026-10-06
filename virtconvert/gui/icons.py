"""Stroke icons drawn for this app (24×24, rendered in the theme's colours).

The line icons of the EZP2019Linux / DiskHealth / CloneAndRecover suite, plus a few of
VirtConvert&Create's own (convert, merge, drop).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_PATHS: dict[str, str] = {
    "open": '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l2 2h8.5A1.5 1.5 0 0 1 21 9.5v9A1.5 1.5 0 0 1 19.5 20h-15A1.5 1.5 0 0 1 3 18.5z"/><path d="M3 11h18"/>',
    "save": '<path d="M5 4h11l4 4v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"/><path d="M8 4v4.5h7V4"/><rect x="7" y="13" width="10" height="7" rx="1"/>',
    "verify": '<path d="M12 3.2 19 6v5.6c0 4.3-2.9 7.6-7 9.2-4.1-1.6-7-4.9-7-9.2V6z"/><path d="m8.8 12 2.2 2.2 4.2-4.4"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.6v.1"/>',
    "cancel": '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M2.5 12h2M19.5 12h2M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    "moon": '<path d="M20 14.6A8.2 8.2 0 0 1 9.4 4a8.2 8.2 0 1 0 10.6 10.6z"/>',
    "log": '<path d="m5 16.5 5-4.5-5-4.5"/><path d="M12.5 18H19"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "trash": '<path d="M4.5 7h15"/><path d="M10 11v5.5M14 11v5.5"/><path d="m6.5 7 .9 11.6a1.6 1.6 0 0 0 1.6 1.4h6a1.6 1.6 0 0 0 1.6-1.4L17.5 7"/><path d="M9.5 7V4.5h5V7"/>',
    "copy": '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="1.8"/><path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19.5 7"/>',
    "warning": '<path d="M10.3 4.4 2.9 17.5A2 2 0 0 0 4.6 20.5h14.8a2 2 0 0 0 1.7-3L13.7 4.4a2 2 0 0 0-3.4 0z"/><path d="M12 9.5v4.5"/><path d="M12 17.2v.1"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.3v.1"/>',
    "refresh": '<path d="M20 11.5A8 8 0 0 0 5.6 7.2L4 9"/><path d="M4 4.5V9h4.5"/><path d="M4 12.5a8 8 0 0 0 14.4 4.3L20 15"/><path d="M20 19.5V15h-4.5"/>',
    "chevron_down": '<path d="m6.5 9.5 5.5 5.5 5.5-5.5"/>',
    "chevron_up": '<path d="m6.5 14.5 5.5-5.5 5.5 5.5"/>',
    "shield": '<path d="M12 3.2 19 6v5.6c0 4.3-2.9 7.6-7 9.2-4.1-1.6-7-4.9-7-9.2V6z"/><path d="M12 8.5v4"/><path d="M12 15.6v.1"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
    "close": '<path d="M6 6l12 12M18 6 6 18"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.8-4.8"/>',
    "edit": '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z"/><path d="m14.5 7.5 2 2"/>',
    "folder": '<path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4.2l2 2H19a1.5 1.5 0 0 1 1.5 1.5v8A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5z"/>',
    "hdd": '<rect x="4" y="3" width="16" height="18" rx="2.2"/><circle cx="12" cy="10" r="4.2"/><circle cx="12" cy="10" r=".8"/><path d="m12.6 10.6 3.4 5.4"/><path d="M7.5 18h1"/>',
    "arrow_right": '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    "file_image": '<path d="M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v5h5"/><circle cx="12.5" cy="14.5" r="3"/><circle cx="12.5" cy="14.5" r=".6"/>',
    "layers": '<path d="m12 4 8.5 4.5L12 13 3.5 8.5z"/><path d="m3.5 12.5 8.5 4.5 8.5-4.5"/><path d="m3.5 16.3 8.5 4.2 8.5-4.2"/>',
    "play": '<path d="M7.5 5.2v13.6a.8.8 0 0 0 1.2.7l10.6-6.8a.8.8 0 0 0 0-1.4L8.7 4.5a.8.8 0 0 0-1.2.7z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.1M4.5 12h.1M4.5 18h.1"/>',
    "eraser": '<path d="m15.5 4.5 4 4a1.4 1.4 0 0 1 0 2L11 19H7l-3-3a1.4 1.4 0 0 1 0-2l9.5-9.5a1.4 1.4 0 0 1 2 0z"/><path d="m9 9.5 5.5 5.5"/><path d="M11 19h9"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "virtual": '<rect x="3.5" y="4" width="17" height="12" rx="1.8"/><path d="M8 20h8M12 16v4"/><path d="m9.5 8.5 2.5 2.5 2.5-2.5"/>',
    "sliders": '<path d="M4 8h8M16 8h4M4 16h4M12 16h8"/><circle cx="14" cy="8" r="2.2"/><circle cx="8" cy="16" r="2.2"/>',
    "wrench": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L4 16.8V20h3.2l5.3-5.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.5-.6-.6-2.5z"/>',
    "shield_check": '<path d="M12 3.2 19 6v5.6c0 4.3-2.9 7.6-7 9.2-4.1-1.6-7-4.9-7-9.2V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 14.6a1.5 1.5 0 0 0 .3 1.6l.1.1a1.8 1.8 0 1 1-2.6 2.6l-.1-.1a1.5 1.5 0 0 0-1.6-.3 1.5 1.5 0 0 0-.9 1.4v.2a1.8 1.8 0 1 1-3.6 0v-.1a1.5 1.5 0 0 0-1-1.4 1.5 1.5 0 0 0-1.6.3l-.1.1a1.8 1.8 0 1 1-2.6-2.6l.1-.1a1.5 1.5 0 0 0 .3-1.6 1.5 1.5 0 0 0-1.4-.9h-.2a1.8 1.8 0 1 1 0-3.6h.1a1.5 1.5 0 0 0 1.4-1 1.5 1.5 0 0 0-.3-1.6l-.1-.1a1.8 1.8 0 1 1 2.6-2.6l.1.1a1.5 1.5 0 0 0 1.6.3h.1a1.5 1.5 0 0 0 .9-1.4v-.2a1.8 1.8 0 1 1 3.6 0v.1a1.5 1.5 0 0 0 .9 1.4 1.5 1.5 0 0 0 1.6-.3l.1-.1a1.8 1.8 0 1 1 2.6 2.6l-.1.1a1.5 1.5 0 0 0-.3 1.6v.1a1.5 1.5 0 0 0 1.4.9h.2a1.8 1.8 0 1 1 0 3.6h-.1a1.5 1.5 0 0 0-1.4.9z"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "minus": '<path d="M5 12h14"/>',
    "partition": '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M9.5 7v10M15 7v10"/>',
    "archive": '<rect x="3" y="4" width="18" height="4.5" rx="1.2"/><path d="M5 8.5v10a1.5 1.5 0 0 0 1.5 1.5h11a1.5 1.5 0 0 0 1.5-1.5v-10"/><path d="M10 12.5h4"/>',
    "convert": '<path d="M4 8h13"/><path d="m13.5 4.5 3.5 3.5-3.5 3.5"/><path d="M20 16H7"/><path d="m10.5 12.5-3.5 3.5 3.5 3.5"/>',
    "merge": '<path d="M6 4v4a4 4 0 0 0 4 4h4a4 4 0 0 1 4 4v4"/><path d="M18 4v4a4 4 0 0 1-4 4"/><path d="m15 17 3 3 3-3"/>',
    "drop": '<path d="M12 4v10"/><path d="m8 10 4 4 4-4"/><path d="M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15"/>',
}

def svg(name: str, color: str, stroke: float = 1.9) -> str:
    body = _PATHS[name]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</svg>')


def _render(svg_text: str, size: int, ratio: float = 2.0) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg_text.encode()))
    px = QPixmap(int(size * ratio), int(size * ratio))
    px.fill(Qt.transparent)
    painter = QPainter(px)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px.width(), px.height()))
    painter.end()
    px.setDevicePixelRatio(ratio)
    return px


@lru_cache(maxsize=512)
def icon(name: str, color: str, disabled_color: str | None = None, size: int = 24) -> QIcon:
    ic = QIcon()
    ic.addPixmap(_render(svg(name, color), size), QIcon.Normal)
    if disabled_color:
        ic.addPixmap(_render(svg(name, disabled_color), size), QIcon.Disabled)
    return ic


def pixmap(name: str, color: str, size: int = 20) -> QPixmap:
    return _render(svg(name, color), size)


def write_svg_file(directory: Path, name: str, color: str, stroke: float = 2.4) -> Path:
    """Write an icon to disk so style sheets can reference it with url()."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}-{color.lstrip('#')}.svg"
    if not path.exists():
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(svg(name, color, stroke), encoding="utf-8")
        os.replace(tmp, path)
    return path
