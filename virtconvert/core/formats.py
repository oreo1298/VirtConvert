"""The disk image formats VirtConvert reads and writes."""

from __future__ import annotations

from dataclasses import dataclass

from .util import GIB, TIB


@dataclass(frozen=True)
class Format:
    key: str                  # our name, used on the command line
    label: str                # short display name
    driver: str               # qemu-img format name
    extension: str            # extension of files we write
    extensions: tuple[str, ...]  # extensions we recognise
    title: str                # one-line description
    works_with: tuple[str, ...]  # hypervisors that open it directly
    max_size: int | None = None
    max_label: str = ""
    can_be_fixed: bool = True
    writable: bool = True


VDI = Format("vdi", "VDI", "vdi", ".vdi", (".vdi",),
             "VirtualBox's own format. Snapshots, compacting and resizing all work.",
             ("VirtualBox",))
VMDK = Format("vmdk", "VMDK", "vmdk", ".vmdk", (".vmdk",),
              "VMware's format, also fully supported by VirtualBox.",
              ("VirtualBox", "VMware"), can_be_fixed=False)
VHD = Format("vhd", "VHD", "vpc", ".vhd", (".vhd", ".avhd"),
             "The older Microsoft format: Hyper-V generation 1, Virtual PC, Azure. "
             "VirtualBox can use it too.",
             ("Hyper-V", "VirtualBox", "Azure"), max_size=2040 * GIB, max_label="2040 GB")
VHDX = Format("vhdx", "VHDX", "vhdx", ".vhdx", (".vhdx", ".avhdx"),
              "Hyper-V's current format, for generation 1 and 2 virtual machines.",
              ("Hyper-V",), max_size=64 * TIB, max_label="64 TB")
QCOW2 = Format("qcow2", "QCOW2", "qcow2", ".qcow2", (".qcow2", ".qcow"),
               "QEMU/KVM's format (virt-manager, Proxmox, GNOME Boxes).",
               ("QEMU/KVM",))
RAW = Format("raw", "Raw", "raw", ".img", (".img", ".raw", ".bin", ".dd"),
             "A plain byte-for-byte disk image, for dd and every other tool.",
             ("QEMU/KVM", "dd"), can_be_fixed=False)

ALL: tuple[Format, ...] = (VDI, VMDK, VHD, VHDX, QCOW2, RAW)
BY_KEY = {f.key: f for f in ALL}
# The formats offered in the app: VirtualBox's and Hyper-V's.
GUI_TARGETS: tuple[Format, ...] = (VDI, VMDK, VHD, VHDX)
HYPERV_FORMATS = (VHD, VHDX)

ALIASES = {
    "vpc": VHD, "virtualbox": VDI, "vbox": VDI, "hyperv": VHDX, "hyper-v": VHDX,
    "vmware": VMDK, "img": RAW, "qcow": QCOW2, "kvm": QCOW2, "qemu": QCOW2,
}


def by_name(name: str) -> Format | None:
    key = name.lower().lstrip(".")
    return BY_KEY.get(key) or ALIASES.get(key)


def by_extension(path: str) -> Format | None:
    lowered = path.lower()
    for fmt in ALL:
        if any(lowered.endswith(ext) for ext in fmt.extensions):
            return fmt
    return None


def default_target_for(source: Format | None) -> Format:
    """Hyper-V disks go to VirtualBox; everything else goes back to Hyper-V."""
    return VDI if source in HYPERV_FORMATS else VHDX
