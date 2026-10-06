"""The disk image formats VertConvert&Create reads and writes."""

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
    works_with: tuple[str, ...]  # programs that open it directly
    max_size: int | None = None
    max_label: str = ""
    can_be_fixed: bool = True
    writable: bool = True
    container: bool = False   # holds disks plus other files (OVA)


VDI = Format("vdi", "VDI", "vdi", ".vdi", (".vdi",),
             "VirtualBox's own format. Snapshots, compacting and resizing all work.",
             ("VirtualBox",))
VMDK = Format("vmdk", "VMDK", "vmdk", ".vmdk", (".vmdk",),
              "VMware's format, also fully supported by VirtualBox.",
              ("VirtualBox", "VMware"))
VHD = Format("vhd", "VHD", "vpc", ".vhd", (".vhd", ".avhd"),
             "The older Microsoft format: Hyper-V generation 1, Virtual PC, Azure. "
             "VirtualBox can use it too.",
             ("Hyper-V", "VirtualBox", "Azure"), max_size=2040 * GIB, max_label="2040 GB")
VHDX = Format("vhdx", "VHDX", "vhdx", ".vhdx", (".vhdx", ".avhdx"),
              "Hyper-V's current format, for generation 1 and 2 virtual machines.",
              ("Hyper-V",), max_size=64 * TIB, max_label="64 TB")
QCOW2 = Format("qcow2", "QCOW2", "qcow2", ".qcow2", (".qcow2",),
               "QEMU/KVM's format (virt-manager, Proxmox, GNOME Boxes, UTM).",
               ("QEMU/KVM", "Proxmox"))
QED = Format("qed", "QED", "qed", ".qed", (".qed",),
             "QEMU's enhanced disk format; VirtualBox can use it too.",
             ("QEMU/KVM", "VirtualBox"), can_be_fixed=False)
OVA = Format("ova", "OVA", "vmdk", ".ova", (".ova",),
             "A virtual appliance: the disk plus a description of the machine, for "
             "File → Import Appliance in VirtualBox and VMware.",
             ("VirtualBox", "VMware"), can_be_fixed=False, container=True)
RAW = Format("raw", "Raw", "raw", ".img", (".img", ".raw", ".bin", ".dd"),
             "A plain byte-for-byte disk image, for dd and every other tool.",
             ("QEMU/KVM", "dd"))
# read only
QCOW = Format("qcow", "QCOW", "qcow", ".qcow", (".qcow",),
              "QEMU's old format (version 1).", ("QEMU",), writable=False)
PARALLELS = Format("parallels", "Parallels", "parallels", ".hds", (".hds", ".hdd"),
                   "Parallels Desktop's disk format.", ("Parallels",), writable=False)
DMG = Format("dmg", "DMG", "dmg", ".dmg", (".dmg",),
             "An Apple disk image (macOS installers and archives).", ("macOS",), writable=False)
ISO = Format("iso", "ISO", "raw", ".iso", (".iso",),
             "A CD/DVD image, read as a disk.", ("every hypervisor",), writable=False)
OVF = Format("ovf", "OVF", "vmdk", ".ovf", (".ovf",),
             "The description of an appliance whose disks are separate files.",
             ("VirtualBox", "VMware"), writable=False, container=True)

ALL: tuple[Format, ...] = (VDI, VMDK, VHD, VHDX, QCOW2, QED, OVA, RAW, QCOW, PARALLELS, DMG, ISO, OVF)
WRITABLE: tuple[Format, ...] = tuple(f for f in ALL if f.writable)
BY_KEY = {f.key: f for f in ALL}
# The targets offered in the app, in two rows of four.
GUI_TARGETS: tuple[Format, ...] = (VDI, VMDK, VHD, VHDX, QCOW2, QED, OVA, RAW)
HYPERV_FORMATS = (VHD, VHDX)
VIRTUALBOX_FORMATS = (VDI, VMDK)

ALIASES = {
    "vpc": VHD, "virtualbox": VDI, "vbox": VDI, "hyperv": VHDX, "hyper-v": VHDX,
    "vmware": VMDK, "img": RAW, "kvm": QCOW2, "qemu": QCOW2, "proxmox": QCOW2,
    "appliance": OVA, "hds": PARALLELS, "hdd": PARALLELS,
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
    """VirtualBox's own disks go to Hyper-V (VHDX); everything else becomes a VDI for VirtualBox."""
    return VHDX if source in VIRTUALBOX_FORMATS else VDI
