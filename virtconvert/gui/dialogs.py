"""Dialogs: about, qemu-img missing, and the yes/no question."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..core import system
from .theme import mono_font
from .widgets import scaled_font


def primary(button: QPushButton) -> QPushButton:
    button.setProperty("variant", "primary")
    return button


def ask_yes_no(parent: QWidget, title: str, text: str, yes: str = "Continue",
               danger: bool = False, informative: str = "") -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    ok = box.addButton(yes, QMessageBox.AcceptRole)
    ok.setProperty("variant", "danger" if danger else "primary")
    box.addButton("Cancel", QMessageBox.RejectRole)
    box.setDefaultButton(ok)
    box.exec()
    return box.clickedButton() is ok


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget, qemu_text: str = ""):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QApplication.windowIcon().pixmap(56, 56))
        head.addWidget(logo, 0, Qt.AlignTop)
        text = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("Title")
        title.setFont(scaled_font(title, 1.6, bold=True))
        version = QLabel(f"Version {__version__} for Linux")
        version.setObjectName("Muted")
        text.addWidget(title)
        text.addWidget(version)
        head.addLayout(text, 1)
        lay.addLayout(head)
        body = QLabel(
            "Converts Hyper-V disks (VHD, VHDX) to formats VirtualBox uses (VDI, VMDK) and back. "
            "Hyper-V checkpoints and VirtualBox snapshots are merged with their parents, and every "
            "copy can be compared with its source before it is saved.<br><br>"
            "The conversion itself is done by <b>qemu-img</b> from the QEMU project. VirtConvert "
            "reads the image headers, finds parents and merges differencing images itself.<br><br>"
            "Released under the MIT license. Not affiliated with Microsoft, Oracle or VMware.")
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        lay.addWidget(body)
        if qemu_text:
            info = QLabel(qemu_text)
            info.setObjectName("Banner")
            info.setWordWrap(True)
            info.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(info)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        lay.addWidget(buttons)


class QemuMissingDialog(QDialog):
    """qemu-img isn't installed: show the command, offer to run it."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("qemu-img is needed")
        self.setMinimumWidth(560)
        self.install_requested = False
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        title = QLabel("Install qemu-img to convert images")
        title.setObjectName("Title")
        title.setFont(scaled_font(title, 1.25, bold=True))
        lay.addWidget(title)
        text = QLabel(
            "VirtConvert uses qemu-img from the QEMU project to write the converted disks. "
            f"It is a small package on {system.distro_name()}. Install it with:")
        text.setWordWrap(True)
        lay.addWidget(text)
        row = QHBoxLayout()
        self.command = QLineEdit(system.qemu_install_command())
        self.command.setReadOnly(True)
        self.command.setFont(mono_font())
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.command.text()))
        row.addWidget(self.command, 1)
        row.addWidget(copy)
        lay.addLayout(row)
        hint = QLabel("Or let VirtConvert run it for you (it asks for your password).")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        buttons = QDialogButtonBox()
        self.install_btn = primary(QPushButton("Install now"))
        buttons.addButton(self.install_btn, QDialogButtonBox.AcceptRole)
        buttons.addButton("Later", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._install)
        buttons.rejected.connect(self.reject)
        can_install = system.package_manager() in system.QEMU_PACKAGES
        self.install_btn.setVisible(can_install)
        hint.setVisible(can_install)
        lay.addWidget(buttons)

    def _install(self) -> None:
        self.install_requested = True
        self.accept()
