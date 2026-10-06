"""Choosing a target format: the format grid, its variants and where it opens."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..core import formats as F
from ..core.convert import Target
from .widgets import SegmentedControl, badge, set_badge

HYPERVISORS = ("VirtualBox", "Hyper-V", "VMware", "QEMU/KVM", "Azure")
ONE_KIND = {
    F.OVA: "A stream-optimized VMDK in an appliance",
    F.QED: "Grows as data is written",
}


class TargetPicker(QWidget):
    """Format (two rows of four), its variant, Azure and 2 GB options, and the hypervisors
    that open it. changed is emitted when the user changes any of them."""

    changed = Signal()

    def __init__(self, parent=None, label_width: int = 74, stream: bool = True):
        super().__init__(parent)
        self._quiet = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self.fmt_seg = SegmentedControl(columns=4)
        self.fmt_seg.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        for fmt in F.GUI_TARGETS:
            self.fmt_seg.add(fmt.label, fmt.key, fmt.title)
        self.fmt_seg.changed.connect(lambda _v: self._edited())
        lay.addWidget(self.fmt_seg)
        self.fmt_desc = QLabel()
        self.fmt_desc.setObjectName("Muted")
        self.fmt_desc.setWordWrap(True)
        lay.addWidget(self.fmt_desc)

        works = QHBoxLayout()
        works.setSpacing(5)
        self.works_label = QLabel("Opens in")
        self.works_label.setObjectName("Muted")
        works.addWidget(self.works_label)
        works.addSpacing(2)
        self.works_badges = {}
        for name in HYPERVISORS:
            b = badge(name, "Idle")
            self.works_badges[name] = b
            works.addWidget(b)
        works.addStretch(1)
        lay.addLayout(works)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.variant_label = QLabel("Variant")
        self.variant_label.setObjectName("Muted")
        self.variant_label.setMinimumWidth(label_width)
        self.alloc_seg = SegmentedControl()
        self.alloc_seg.add("Dynamic", "dynamic", "The file grows as the guest writes data")
        self.alloc_seg.add("Fixed", "fixed", "The whole disk is allocated now (slightly faster)")
        self.alloc_seg.changed.connect(lambda _v: self._edited())
        self.vmdk_seg = SegmentedControl()
        self.vmdk_seg.add("Dynamic", "dynamic", "A VMDK that grows as the guest writes data")
        self.vmdk_seg.add("Fixed", "fixed", "Allocated now: a small descriptor plus a -flat data file")
        if stream:
            self.vmdk_seg.add("Stream", "stream", "Stream-optimized: compressed, for VMware ESXi and "
                                                  "appliances (VirtualBox imports it but can't run from it)")
        self.vmdk_seg.changed.connect(lambda _v: self._edited())
        self.variant_text = QLabel()
        self.variant_text.setObjectName("Muted")
        row.addWidget(self.variant_label)
        row.addWidget(self.alloc_seg, 1)
        row.addWidget(self.vmdk_seg, 1)
        row.addWidget(self.variant_text, 1)
        lay.addLayout(row)
        self.split_chk = QCheckBox("In 2 GB files (VMware Workstation's way)")
        self.split_chk.toggled.connect(lambda _c: self._edited())
        lay.addWidget(self.split_chk)
        self.azure_chk = QCheckBox("For Azure (fixed, whole megabytes)")
        self.azure_chk.toggled.connect(lambda _c: self._edited())
        lay.addWidget(self.azure_chk)
        self.set_target(Target(F.VDI))

    def _edited(self) -> None:
        if not self._quiet:
            self.set_target(self.target())      # show what the choice amounts to (Azure is fixed, ...)
            self.changed.emit()

    def target(self) -> Target:
        fmt = F.BY_KEY.get(self.fmt_seg.value(), F.VDI)
        if fmt is F.VMDK:
            variant = self.vmdk_seg.value()
            return Target(fmt, fixed=variant == "fixed", stream=variant == "stream",
                          split=self.split_chk.isChecked()).normalized()
        return Target(fmt, fixed=self.alloc_seg.value() == "fixed",
                      azure=self.azure_chk.isChecked() and fmt is F.VHD).normalized()

    def set_target(self, target: Target) -> None:
        self._quiet = True
        try:
            if target.format in F.GUI_TARGETS:
                self.fmt_seg.set_value(target.format.key)
            self.alloc_seg.set_value("fixed" if target.fixed else "dynamic")
            self.vmdk_seg.set_value("stream" if target.stream else "fixed" if target.fixed else "dynamic")
            self.split_chk.setChecked(target.split)
            self.azure_chk.setChecked(target.azure)
        finally:
            self._quiet = False
        self._show(self.target())       # what the controls amount to (a stored "stream" etc.)

    def _show(self, target: Target) -> None:
        fmt = target.format
        self.fmt_desc.setText(fmt.title)
        for name, b in self.works_badges.items():
            # only the ones that open it: all five don't fit next to each other
            works = name in fmt.works_with or (name == "Azure" and target.azure)
            set_badge(b, name, "Ok")
            b.setVisible(works)
        is_vmdk = fmt is F.VMDK
        has_alloc = fmt.can_be_fixed and not is_vmdk
        self.alloc_seg.setVisible(has_alloc)
        self.vmdk_seg.setVisible(is_vmdk)
        self.variant_text.setVisible(not has_alloc and not is_vmdk)
        self.variant_text.setText(ONE_KIND.get(fmt, ""))
        self.alloc_seg.set_item_enabled("dynamic", not target.azure)
        self.split_chk.setVisible(is_vmdk)
        self.split_chk.setEnabled(not target.stream)
        self.azure_chk.setVisible(fmt is F.VHD)

    def variant_key(self) -> str:
        """The variant as stored in the settings: 'fixed', 'stream', 'azure', 'split', 'fixed+split'."""
        t = self.target()
        parts = ["azure"] if t.azure else ["stream"] if t.stream else ["fixed"] if t.fixed else []
        if t.split:
            parts.append("split")
        return "+".join(parts)


def target_from_setting(text: str, default: F.Format) -> Target:
    """The Target stored as 'vmdk:fixed+split' (see TargetPicker.variant_key)."""
    fmt_key, _, variant = str(text).partition(":")
    fmt = F.BY_KEY.get(fmt_key)
    if fmt not in F.GUI_TARGETS:
        fmt = default
    parts = set(variant.split("+"))
    return Target(fmt, fixed="fixed" in parts, stream="stream" in parts, azure="azure" in parts,
                  split="split" in parts).normalized()
