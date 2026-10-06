"""Recognise the partition table at the start of a virtual disk.

Used to tell whether the guest boots with UEFI (GPT) or BIOS (MBR), which
decides "Enable EFI" in VirtualBox and the generation of a Hyper-V VM.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field

GPT_TYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI system",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft reserved",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Basic data",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows recovery",
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3": "Windows LDM metadata",
    "af9b60a0-1431-4f62-bc68-3311714a69ad": "Windows LDM data",
    "e75caf8f-f680-4cee-afa3-b001e56efc2d": "Storage Spaces",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "Linux root (x86-64)",
    "44479540-f297-41b2-9af7-d131d5f0458a": "Linux root (x86)",
    "b921b045-1df0-41c3-af44-4c6f280d3fae": "Linux root (ARM64)",
    "933ac7e1-2eb4-4f13-b844-0e14e2aef915": "Linux home",
    "bc13c2ff-59e6-4262-a352-b275fd6f7172": "Linux /boot",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "a19d880f-05fc-4d3b-a006-743f0f84911e": "Linux RAID",
    "ca7d7ccb-63ed-4c53-861c-1742536059cc": "Linux LUKS",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
    "48465300-0000-11aa-aa11-00306543ecac": "Apple HFS+",
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Apple APFS",
    "516e7cb6-6ecf-11d6-8ff8-00022d09712b": "FreeBSD UFS",
    "516e7cba-6ecf-11d6-8ff8-00022d09712b": "FreeBSD ZFS",
    "6a898cc3-1dd2-11b2-99a6-080020736631": "ZFS",
}
MBR_TYPES = {
    0x01: "FAT12", 0x04: "FAT16", 0x05: "Extended", 0x06: "FAT16", 0x07: "NTFS/exFAT",
    0x0B: "FAT32", 0x0C: "FAT32", 0x0E: "FAT16", 0x0F: "Extended", 0x11: "Hidden FAT",
    0x12: "Recovery", 0x17: "Hidden NTFS", 0x1B: "Hidden FAT32", 0x1C: "Hidden FAT32",
    0x27: "Windows recovery", 0x42: "Windows dynamic", 0x82: "Linux swap", 0x83: "Linux",
    0x85: "Extended", 0x8E: "Linux LVM", 0xA5: "FreeBSD", 0xA6: "OpenBSD", 0xA9: "NetBSD",
    0xAF: "Apple HFS", 0xEE: "GPT protective", 0xEF: "EFI system", 0xFD: "Linux RAID",
}


@dataclass
class Partition:
    number: int
    start: int   # bytes
    size: int    # bytes
    kind: str
    name: str = ""
    bootable: bool = False


@dataclass
class PartitionTable:
    scheme: str                     # "gpt", "mbr" or "none"
    sector_size: int = 512
    partitions: list[Partition] = field(default_factory=list)
    has_boot_code: bool = False

    @property
    def firmware(self) -> str:
        """'uefi', 'bios', 'both' (GPT with a BIOS boot partition) or '' (unknown)."""
        kinds = {p.kind for p in self.partitions}
        if self.scheme == "gpt":
            if "BIOS boot" in kinds:
                return "both"
            return "uefi"
        if self.scheme == "mbr":
            if "EFI system" in kinds and not self.has_boot_code:
                return "uefi"
            return "bios"
        return ""

    @property
    def label(self) -> str:
        if self.scheme == "none":
            return "No partition table"
        name = {"gpt": "GPT", "mbr": "MBR"}[self.scheme]
        count = len(self.partitions)
        return f"{name}, {count} partition{'s' if count != 1 else ''}"

    @property
    def boot_label(self) -> str:
        return {"uefi": "UEFI", "bios": "BIOS (legacy)", "both": "BIOS or UEFI"}.get(self.firmware, "—")

    def summary(self) -> str:
        names = []
        for p in self.partitions:
            text = p.kind
            if p.name and p.name.lower() not in p.kind.lower():
                text += f" “{p.name}”"
            names.append(text)
        return ", ".join(names)


def analyze(head: bytes, logical_sector: int | None = None) -> PartitionTable:
    """Look at the first sectors of a disk (at least 64 KiB for GPT entries)."""
    sizes = [logical_sector] if logical_sector else [512, 4096]
    for sector in sizes:
        table = _gpt(head, sector)
        if table is not None:
            return table
    if len(head) >= 512 and head[510:512] == b"\x55\xaa":
        table = _mbr(head)
        if table is not None:
            return table
    return PartitionTable("none")


def _gpt(head: bytes, sector: int) -> PartitionTable | None:
    if len(head) < 2 * sector or head[sector:sector + 8] != b"EFI PART":
        return None
    entries_lba, count, entry_size = struct.unpack_from("<QII", head, sector + 72)
    table = PartitionTable("gpt", sector_size=sector, has_boot_code=any(head[:440]))
    if entry_size < 128 or count > 1024:
        return table
    start = entries_lba * sector
    for i in range(count):
        off = start + i * entry_size
        if off + 128 > len(head):
            break
        type_guid = uuid.UUID(bytes_le=bytes(head[off:off + 16]))
        if type_guid.int == 0:
            continue
        first, last = struct.unpack_from("<QQ", head, off + 32)
        name = bytes(head[off + 56:off + 128]).decode("utf-16-le", "replace").split("\0", 1)[0]
        kind = GPT_TYPES.get(str(type_guid), "Other")
        table.partitions.append(Partition(i + 1, first * sector, (last - first + 1) * sector, kind, name))
    return table


def _mbr(head: bytes) -> PartitionTable | None:
    table = PartitionTable("mbr", has_boot_code=any(head[:440]))
    for i in range(4):
        off = 446 + 16 * i
        status, ptype = head[off], head[off + 4]
        lba, sectors = struct.unpack_from("<II", head, off + 8)
        if status not in (0, 0x80):
            return None  # not a partition table (a filesystem boot sector, for example)
        if ptype == 0 or sectors == 0:
            continue
        table.partitions.append(Partition(i + 1, lba * 512, sectors * 512,
                                          MBR_TYPES.get(ptype, f"Type {ptype:02X}"),
                                          bootable=status == 0x80))
    if not table.partitions:
        return None  # a filesystem written to the whole disk, or an empty MBR
    return table
