"""GUI entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication

from .. import APP_ID, APP_NAME, DESKTOP_ID, __version__
from ..core.qemu import QemuImg
from .main_window import MainWindow
from .theme import theme

ICON_PATH = Path(__file__).resolve().parent.parent / "data" / "virtconvert.svg"


def app_icon() -> QIcon:
    themed = QIcon.fromTheme(DESKTOP_ID)
    if not themed.isNull():
        return themed
    return QIcon(str(ICON_PATH))


def run_gui(files: list[str] | None = None, argv: list[str] | None = None) -> int:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setApplicationName(APP_ID)
    QApplication.setApplicationDisplayName(APP_NAME)
    QApplication.setApplicationVersion(__version__)
    QApplication.setOrganizationName(APP_ID)
    QGuiApplication.setDesktopFileName(DESKTOP_ID)
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv[:1])
    app.setWindowIcon(app_icon())

    settings = QSettings(APP_ID, APP_ID)
    mode = os.environ.get("VIRTCONVERT_THEME") or str(settings.value("theme", "system"))
    theme.apply(app, mode)
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(
            lambda _s: theme.apply(app) if theme.mode == "system" else None)

    window = MainWindow(QemuImg.find())
    window.show()
    if files:
        QTimer.singleShot(0, lambda: window.add_files([str(f) for f in files]))
    elif window.qemu is None:
        QTimer.singleShot(300, window.offer_qemu_install)
    return app.exec()
