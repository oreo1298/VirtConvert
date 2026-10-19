"""Partition tables: recognise the one at the start of a virtual disk, and write new ones.

Reading tells whether the guest boots with UEFI (GPT) or BIOS (MBR), which
decides "Enable EFI" in VirtualBox and the generation of a Hyper-V VM.
Writing makes the GPT or MBR of a new disk (create.py) or of a copy of some
partitions of a physical disk (devices.PartitionReader, which reads the disk's
own entries with read_entries), and moves a GPT's backup to the end of a
physical disk of another size than the virtual disk written onto it
(writedisk.py).
"""

from __future__ import annotations

import os
import struct
import uuid
import zlib
from dataclasses import dataclass, field
from typing import Callable

BASIC_DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
LINUX_DATA = "0fc63daf-8483-4772-8e79-3d69d8477de4"
ALIGN = 1 << 20             # partitions start and end on whole megabytes
SECTOR = 512
GPT_ENTRIES = 128

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
    entries_bytes: int = 0          # a GPT's entry array (its backup needs that much at the disk's end)
    positions_known: bool = True    # false when only the sizes are known (lsblk without /sys)

    @property
    def firmware(self) -> str:
        """'uefi', 'bios', 'both' (GPT with a BIOS boot partition) or '' (unknown)."""
        kinds = {p.kind for p in self.partitions}
        if self.scheme == "gpt":
            if "BIOS boot" in kinds:
                return "both"
            # a GPT disk boots with UEFI from its EFI system partition; without one it's a data disk
            return "uefi" if "EFI system" in kinds or self.has_boot_code else ""
        if self.scheme == "mbr":
            if "EFI system" in kinds and not self.has_boot_code:
                return "uefi"
            if not self.has_boot_code and not any(p.bootable for p in self.partitions):
                return ""      # no boot code and nothing active: a data disk
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
        # an MBR counts in the disk's sectors: 512 bytes unless the disk is known to use 4 KB ones
        table = _mbr(head, logical_sector or 512)
        if table is not None:
            return table
    return PartitionTable("none")


def _gpt(head: bytes, sector: int) -> PartitionTable | None:
    if len(head) < 2 * sector or head[sector:sector + 8] != b"EFI PART":
        return None
    entries_lba, count, entry_size = struct.unpack_from("<QII", head, sector + 72)
    table = PartitionTable("gpt", sector_size=sector, has_boot_code=any(head[:440]),
                           entries_bytes=count * entry_size)
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


def _mbr(head: bytes, sector: int = 512) -> PartitionTable | None:
    table = PartitionTable("mbr", sector_size=sector, has_boot_code=any(head[:440]))
    for i in range(4):
        off = 446 + 16 * i
        status, ptype = head[off], head[off + 4]
        lba, sectors = struct.unpack_from("<II", head, off + 8)
        if status not in (0, 0x80):
            return None  # not a partition table (a filesystem boot sector, for example)
        if ptype == 0 or sectors == 0:
            continue
        table.partitions.append(Partition(i + 1, lba * sector, sectors * sector,
                                          MBR_TYPES.get(ptype, f"Type {ptype:02X}"),
                                          bootable=status == 0x80))
    if not table.partitions:
        return None  # a filesystem written to the whole disk, or an empty MBR
    return table


# ---------------------------------------------------------------------- writing

def single_partition(disk_size: int, scheme: str) -> tuple[int, int]:
    """Where one partition filling a new disk goes, as (offset, length) in bytes: from 1 MB
    to the last whole megabyte before the end (GPT keeps its backup table there)."""
    lbas = disk_size // SECTOR
    step = ALIGN // SECTOR
    last_usable = lbas - 34 if scheme == "gpt" else lbas - 1
    end = (last_usable + 1) // step * step
    return ALIGN, max(0, end - step) * SECTOR


def gpt_table(disk_size: int, partitions: list[tuple[int, int, str, str]]) -> dict[int, bytes]:
    """A protective MBR and the primary and backup GPT, as {offset: bytes}.

    PARTITIONS: (offset, length, type GUID, name) in bytes."""
    entries = []
    for offset, length, type_guid, name in partitions:
        entry = bytearray(128)
        struct.pack_into("<16s16sQQQ", entry, 0, uuid.UUID(type_guid).bytes_le, uuid.uuid4().bytes_le,
                         offset // SECTOR, (offset + length) // SECTOR - 1, 0)
        label = _utf16_cut(name, 36).encode("utf-16-le")
        entry[56:56 + len(label)] = label
        entries.append(bytes(entry))
    return gpt_with_entries(disk_size, entries)


def gpt_with_entries(disk_size: int, entries: list[bytes]) -> dict[int, bytes]:
    """A protective MBR and the primary and backup GPT holding ENTRIES (128-byte partition entries
    that already say where their partition is, in 512-byte sectors), as {offset: bytes}."""
    lbas = disk_size // SECTOR
    entries_sectors = GPT_ENTRIES * 128 // SECTOR
    array = bytearray(GPT_ENTRIES * 128)
    for i, entry in enumerate(entries[:GPT_ENTRIES]):
        array[i * 128:(i + 1) * 128] = entry[:128]
    entries = array
    entries_crc = zlib.crc32(entries)
    disk_guid = uuid.uuid4().bytes_le

    def header(current: int, backup: int, entries_lba: int) -> bytes:
        h = bytearray(92)
        struct.pack_into("<8sIIIIQQQQ16sQIII", h, 0, b"EFI PART", 0x00010000, 92, 0, 0, current, backup,
                         2 + entries_sectors, lbas - 2 - entries_sectors, disk_guid, entries_lba,
                         GPT_ENTRIES, 128, entries_crc)
        struct.pack_into("<I", h, 16, zlib.crc32(h))
        return bytes(h).ljust(SECTOR, b"\0")

    protective = mbr_table([(SECTOR, min(lbas - 1, 0xFFFFFFFF) * SECTOR, 0xEE)], signature=False)[0]
    return {
        0: protective,
        SECTOR: header(1, lbas - 1, 2),
        2 * SECTOR: bytes(entries),
        (lbas - 1 - entries_sectors) * SECTOR: bytes(entries),
        (lbas - 1) * SECTOR: header(lbas - 1, 1, lbas - 1 - entries_sectors),
    }


def _utf16_cut(text: str, units: int) -> str:
    """TEXT in at most UNITS UTF-16 code units, without splitting a surrogate pair."""
    kept, used = [], 0
    for c in text:
        size = 2 if ord(c) > 0xFFFF else 1
        if used + size > units:
            break
        kept.append(c)
        used += size
    return "".join(kept)


def _chs(lba: int) -> bytes:
    """The old cylinder/head/sector address (255 heads, 63 sectors), capped like every tool does."""
    cylinder, rest = divmod(lba, 255 * 63)
    head, sector = divmod(rest, 63)
    if cylinder > 1023:
        cylinder, head, sector = 1023, 254, 62
    return bytes((head, (sector + 1) | ((cylinder >> 2) & 0xC0), cylinder & 0xFF))


def mbr_table(partitions: list[tuple], signature: bool = True) -> dict[int, bytes]:
    """An MBR without boot code. PARTITIONS: (offset, length, type[, active]) in bytes, at most four."""
    mbr = bytearray(SECTOR)
    if signature:       # Windows identifies MBR disks by this number
        mbr[440:444] = (int.from_bytes(os.urandom(4), "little") | 1).to_bytes(4, "little")
    for i, (offset, length, ptype, *active) in enumerate(partitions):
        first, count = offset // SECTOR, length // SECTOR
        struct.pack_into("<B3sB3sII", mbr, 446 + 16 * i, 0x80 if active and active[0] else 0, _chs(first), ptype,
                         _chs(first + count - 1), first, count)
    mbr[510:512] = b"\x55\xaa"
    return {0: bytes(mbr)}


EXTENDED_TYPES = (0x05, 0x0F, 0x85)     # an MBR partition that holds the logical ones


@dataclass
class TableEntry:
    """A partition as the disk's own table has it (read_entries)."""
    number: int                  # as Linux counts: GPT entry index + 1; MBR 1-4, logical ones from 5
    start: int                   # bytes
    size: int                    # bytes
    gpt_entry: bytes = b""       # the 128-byte GPT entry (type, unique GUID, attributes, name)
    mbr_type: int = 0
    active: bool = False


def read_entries(read: Callable[[int, int], bytes], sector: int) -> tuple[str, dict[int, TableEntry]]:
    """The partitions of a disk from its own table: ('gpt' | 'mbr' | 'none', {number: entry}).
    READ(offset, length) reads the disk; SECTOR is its logical sector size. MBR logical partitions
    are found through the extended partition's chain of EBRs."""
    head = read(0, 2 * sector)
    found: dict[int, TableEntry] = {}
    if head[sector:sector + 8] == b"EFI PART":
        entries_lba, count, entry_size = struct.unpack_from("<QII", head, sector + 72)
        if entry_size < 128 or not 0 < count <= 1024:
            return "gpt", found
        array = read(entries_lba * sector, count * entry_size)
        for i in range(count):
            raw = array[i * entry_size:i * entry_size + 128]
            if len(raw) < 128 or not any(raw[:16]):
                continue
            first, last = struct.unpack_from("<QQ", raw, 32)
            found[i + 1] = TableEntry(i + 1, first * sector, (last - first + 1) * sector, gpt_entry=bytes(raw))
        return "gpt", found
    if head[510:512] != b"\x55\xaa":
        return "none", found
    extended = None
    for i in range(4):
        e = head[446 + 16 * i:462 + 16 * i]
        ptype, (lba, count) = e[4], struct.unpack_from("<II", e, 8)
        if ptype == 0 or count == 0:
            continue
        if ptype in EXTENDED_TYPES:
            extended = lba
        found[i + 1] = TableEntry(i + 1, lba * sector, count * sector, mbr_type=ptype, active=e[0] == 0x80)
    ebr, number, seen = extended, 5, set()
    while ebr is not None and ebr not in seen and number < 5 + 256:
        seen.add(ebr)
        block = read(ebr * sector, 512)
        if len(block) < 512 or block[510:512] != b"\x55\xaa":
            break
        here, nxt = block[446:462], block[462:478]
        lba, count = struct.unpack_from("<II", here, 8)
        if here[4] and count:
            found[number] = TableEntry(number, (ebr + lba) * sector, count * sector, mbr_type=here[4],
                                       active=here[0] == 0x80)
            number += 1
        next_lba = struct.unpack_from("<I", nxt, 8)[0]
        ebr = extended + next_lba if nxt[4] and next_lba else None
    return "mbr", found


def gpt_room(sector: int, entries_bytes: int = GPT_ENTRIES * 128) -> int:
    """The bytes a GPT needs after the last partition: its backup entries and header."""
    return -(-entries_bytes // sector) * sector + sector


def move_backup_gpt(read: Callable[[int, int], bytes], write: Callable[[int, bytes], None], disk_size: int,
                    sector: int) -> bool:
    """Fit the GPT at the start of a disk to the disk's size, the way `sgdisk -e` does: the backup
    entries and header go to its last sectors, the last usable sector before them, and the
    protective MBR covers the whole disk. Needed after writing a virtual disk onto a disk of
    another size (Linux ignores a GPT whose partitions may lie beyond the disk's end).

    READ(offset, length) and WRITE(offset, data) work on the disk. False when there is nothing
    to do, or no valid GPT."""
    primary = bytearray(read(sector, sector))
    if len(primary) < 92 or primary[:8] != b"EFI PART":
        return False
    header_size, = struct.unpack_from("<I", primary, 12)
    if not 92 <= header_size <= sector:
        return False
    stored_crc, = struct.unpack_from("<I", primary, 16)
    if zlib.crc32(bytes(primary[:16]) + b"\0\0\0\0" + bytes(primary[20:header_size])) != stored_crc:
        return False
    current, old_backup, _first, last_usable = struct.unpack_from("<QQQQ", primary, 24)
    entries_lba, count, entry_size, entries_crc = struct.unpack_from("<QIII", primary, 72)
    if current != 1 or entry_size < 128 or not 0 < count * entry_size <= (1 << 20):
        return False
    entries = read(entries_lba * sector, count * entry_size)
    if len(entries) != count * entry_size or zlib.crc32(entries) != entries_crc:
        return False
    lbas = disk_size // sector
    backup = lbas - 1
    backup_entries = backup - -(-count * entry_size // sector)
    new_last_usable = backup_entries - 1
    used = [struct.unpack_from("<Q", entries, i * entry_size + 40)[0] for i in range(count)
            if any(entries[i * entry_size:i * entry_size + 16])]
    if max(used, default=0) > new_last_usable or backup_entries <= entries_lba:
        return False                # the partitions don't fit (writedisk checks that first)
    if old_backup == backup and last_usable == new_last_usable:
        return False

    def header(this: int, other: int, at: int) -> bytes:
        h = bytearray(primary)
        struct.pack_into("<QQ", h, 24, this, other)
        struct.pack_into("<Q", h, 48, new_last_usable)
        struct.pack_into("<Q", h, 72, at)
        struct.pack_into("<I", h, 16, 0)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h[:header_size])))
        return bytes(h)

    write(backup_entries * sector, bytes(entries))
    write(backup * sector, header(backup, 1, backup_entries))
    write(sector, header(1, backup, entries_lba))
    # the old backup header is now somewhere in the middle of a larger disk: remove it, so
    # partition recovery tools don't take it for a table
    if old_backup < backup_entries and old_backup > entries_lba and read(old_backup * sector, 8) == b"EFI PART":
        write(old_backup * sector, bytes(sector))
    # a protective MBR (one partition of type EE from sector 1) spans the whole disk
    mbr = bytearray(read(0, 512))
    if len(mbr) == 512 and mbr[510:512] == b"\x55\xaa":
        slots = [mbr[446 + 16 * i:462 + 16 * i] for i in range(4)]
        used_slots = [i for i, e in enumerate(slots) if e[4] != 0]
        if len(used_slots) == 1 and slots[used_slots[0]][4] == 0xEE:
            at = 446 + 16 * used_slots[0]
            if struct.unpack_from("<I", mbr, at + 8)[0] == 1:
                struct.pack_into("<I", mbr, at + 12, min(lbas - 1, 0xFFFFFFFF))
                write(0, bytes(mbr))
    return True
