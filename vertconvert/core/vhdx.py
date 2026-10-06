"""VHDX (Hyper-V) headers, metadata and block map, following [MS-VHDX].

Layout (little-endian): a file identifier, two 4 KiB headers (the valid one
with the highest sequence number is current), two region tables pointing at
the block allocation table (BAT) and the metadata region. BAT entries come in
chunks: `chunk ratio` payload block entries followed by one sector bitmap
entry. Sector bitmaps (least significant bit first) say which sectors of a
partially present block live in this file and which in the parent.
"""

from __future__ import annotations

import os
import struct
import sys
import uuid
from array import array
from dataclasses import dataclass, field
from typing import Iterator

from .errors import ImageFormatError, UnsupportedImageError
from .layers import DATA, LRU, PARENT, ZERO, Extent, Layer, bit_runs

SIGNATURE = b"vhdxfile"
MIB = 1 << 20
HEADER_OFFSETS = (64 * 1024, 128 * 1024)
REGION_OFFSETS = (192 * 1024, 256 * 1024)

BAT_REGION = uuid.UUID("2DC27766-F623-4200-9D64-115E9BFD4A08")
METADATA_REGION = uuid.UUID("8B7CA206-4790-4B9A-B8FE-575F050F886E")
FILE_PARAMETERS = uuid.UUID("CAA16737-FA36-4D43-B3B6-33F0AA44E76B")
VIRTUAL_DISK_SIZE = uuid.UUID("2FA54224-CD1B-4876-B211-5DBED83BF4B8")
VIRTUAL_DISK_ID = uuid.UUID("BECA12AB-B2E6-4523-93EF-C309E000C746")
LOGICAL_SECTOR_SIZE = uuid.UUID("8141BF1D-A96F-4709-BA47-F233A8FAAB5F")
PHYSICAL_SECTOR_SIZE = uuid.UUID("CDA348C7-445D-4471-9CC9-E9885251C556")
PARENT_LOCATOR = uuid.UUID("A8D35F2D-B30B-454D-ABF7-D3D84834AB0C")
VHDX_PARENT_LOCATOR_TYPE = uuid.UUID("B04AEFB7-D19E-4A81-B789-25B8E9445913")
KNOWN_METADATA = {FILE_PARAMETERS, VIRTUAL_DISK_SIZE, VIRTUAL_DISK_ID, LOGICAL_SECTOR_SIZE,
                  PHYSICAL_SECTOR_SIZE, PARENT_LOCATOR}

# payload block states
NOT_PRESENT, UNDEFINED, BLOCK_ZERO, UNMAPPED, UNMAPPED_V095, FULLY_PRESENT, PARTIALLY_PRESENT = 0, 1, 2, 3, 5, 6, 7
SB_PRESENT = 6

_CRC_TABLE: list[int] = []


def crc32c(data: bytes) -> int:
    if not _CRC_TABLE:
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
            _CRC_TABLE.append(c)
    table = _CRC_TABLE
    crc = 0xFFFFFFFF
    for b in data:
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def checksum_ok(block: bytes) -> bool:
    stored = struct.unpack_from("<I", block, 4)[0]
    return crc32c(block[:4] + b"\0\0\0\0" + block[8:]) == stored


def guid(data: bytes, offset: int = 0) -> uuid.UUID:
    return uuid.UUID(bytes_le=bytes(data[offset:offset + 16]))


@dataclass
class Header:
    sequence: int
    file_write_guid: uuid.UUID
    data_write_guid: uuid.UUID
    log_guid: uuid.UUID
    log_version: int
    version: int
    log_length: int
    log_offset: int


@dataclass
class VhdxInfo:
    creator: str
    header: Header
    bat_offset: int
    bat_length: int
    metadata_offset: int
    block_size: int = 0
    leave_allocated: bool = False
    has_parent: bool = False
    virtual_size: int = 0
    disk_id: str = ""
    logical_sector: int = 512
    physical_sector: int = 4096
    parent_locator: dict[str, str] = field(default_factory=dict)
    damaged_copy: list[str] = field(default_factory=list)
    log_entries: int = 0

    @property
    def dirty(self) -> bool:
        """The log holds entries: the file was not closed cleanly and needs a log replay."""
        return self.log_entries > 0

    @property
    def chunk_ratio(self) -> int:
        return (1 << 23) * self.logical_sector // self.block_size

    @property
    def data_blocks(self) -> int:
        return -(-self.virtual_size // self.block_size)

    @property
    def bat_entries(self) -> int:
        cr = self.chunk_ratio
        if self.has_parent:
            return -(-self.data_blocks // cr) * (cr + 1)
        return self.data_blocks + (self.data_blocks - 1) // cr

    @property
    def subformat(self) -> str:
        if self.has_parent:
            return "differencing"
        return "fixed" if self.leave_allocated else "dynamic"

    @property
    def parent_linkage(self) -> str:
        return self.parent_locator.get("parent_linkage", "")

    @property
    def parent_paths(self) -> list[str]:
        keys = ("relative_path", "absolute_win32_path", "volume_path")
        return [self.parent_locator[k] for k in keys if self.parent_locator.get(k)]


def parse(read, file_size: int) -> VhdxInfo:
    """Parse the headers and metadata. READ(offset, length) reads from the file."""
    ident = read(0, 520)
    if ident[:8] != SIGNATURE:
        raise ImageFormatError("not a VHDX file")
    creator = bytes(ident[8:520]).decode("utf-16-le", "replace").split("\0", 1)[0].strip()
    if file_size < 320 * 1024:
        raise ImageFormatError("the VHDX file is truncated (shorter than its headers)")

    damaged: list[str] = []
    headers: list[Header] = []
    for i, off in enumerate(HEADER_OFFSETS):
        block = read(off, 4096)
        if block[:4] == b"head" and checksum_ok(block):
            seq, = struct.unpack_from("<Q", block, 8)
            log_version, version, log_length, log_offset = struct.unpack_from("<HHIQ", block, 64)
            headers.append(Header(seq, guid(block, 16), guid(block, 32), guid(block, 48),
                                  log_version, version, log_length, log_offset))
        else:
            damaged.append(f"header {i + 1}")
    if not headers:
        raise ImageFormatError("both VHDX headers are damaged")
    header = max(headers, key=lambda h: h.sequence)
    if header.version != 1:
        raise UnsupportedImageError(f"VHDX version {header.version} is not supported")
    log_entries = count_log_entries(read, header, file_size)

    regions: dict[uuid.UUID, tuple[int, int]] | None = None
    for i, off in enumerate(REGION_OFFSETS):
        table = read(off, 64 * 1024)
        if table[:4] != b"regi" or not checksum_ok(table):
            damaged.append(f"region table {i + 1}")
            continue
        count, = struct.unpack_from("<I", table, 8)
        if count > 2047:
            damaged.append(f"region table {i + 1}")
            continue
        regions = {}
        for n in range(count):
            e = 16 + 32 * n
            rid = guid(table, e)
            offset, length, flags = struct.unpack_from("<QII", table, e + 16)
            if rid not in (BAT_REGION, METADATA_REGION) and flags & 1:
                raise UnsupportedImageError(f"the VHDX uses an unknown required region {rid}")
            regions[rid] = (offset, length)
        break
    if regions is None:
        raise ImageFormatError("both VHDX region tables are damaged")
    if BAT_REGION not in regions or METADATA_REGION not in regions:
        raise ImageFormatError("the VHDX region table has no block table or metadata")

    meta_off = regions[METADATA_REGION][0]
    info = VhdxInfo(creator=creator, header=header, bat_offset=regions[BAT_REGION][0],
                    bat_length=regions[BAT_REGION][1], metadata_offset=meta_off,
                    damaged_copy=damaged, log_entries=log_entries)
    table = read(meta_off, 64 * 1024)
    if table[:8] != b"metadata":
        raise ImageFormatError("the VHDX metadata table is damaged")
    count, = struct.unpack_from("<H", table, 10)
    items: dict[uuid.UUID, tuple[int, int]] = {}
    for n in range(min(count, 2047)):
        e = 32 + 32 * n
        item = guid(table, e)
        offset, length, flags = struct.unpack_from("<III", table, e + 16)
        if item not in KNOWN_METADATA and flags & 4:
            raise UnsupportedImageError(f"the VHDX uses unknown required metadata {item}")
        items[item] = (offset, length)

    def item(key: uuid.UUID, length: int) -> bytes | None:
        if key not in items:
            return None
        return read(meta_off + items[key][0], length)

    for key in (FILE_PARAMETERS, VIRTUAL_DISK_SIZE, LOGICAL_SECTOR_SIZE):
        if key not in items:
            raise ImageFormatError("the VHDX metadata is incomplete")
    block_size, flags = struct.unpack("<II", item(FILE_PARAMETERS, 8))
    info.block_size = block_size
    info.leave_allocated = bool(flags & 1)
    info.has_parent = bool(flags & 2)
    info.virtual_size, = struct.unpack("<Q", item(VIRTUAL_DISK_SIZE, 8))
    info.logical_sector, = struct.unpack("<I", item(LOGICAL_SECTOR_SIZE, 4))
    data = item(PHYSICAL_SECTOR_SIZE, 4)
    if data:
        info.physical_sector, = struct.unpack("<I", data)
    data = item(VIRTUAL_DISK_ID, 16)
    if data:
        info.disk_id = str(guid(data))
    if info.logical_sector not in (512, 4096):
        raise ImageFormatError(f"invalid VHDX logical sector size {info.logical_sector}")
    if not (MIB <= block_size <= 256 * MIB) or block_size & (block_size - 1):
        raise ImageFormatError(f"invalid VHDX block size {block_size}")
    if info.virtual_size % info.logical_sector:
        raise ImageFormatError("the VHDX disk size is not a whole number of sectors")
    if info.has_parent:
        if PARENT_LOCATOR not in items:
            raise ImageFormatError("the differencing VHDX has no parent locator")
        offset, size = items[PARENT_LOCATOR]
        info.parent_locator = parse_parent_locator(read(meta_off + offset, min(size, 64 * 1024)))
    return info


def count_log_entries(read, header: Header, file_size: int) -> int:
    """Valid entries in the log ([MS-VHDX] 2.3).

    A LogGuid alone doesn't mean the log has to be replayed: only entries carrying that
    GUID, with a correct checksum, do (qemu-img decides the same way).
    """
    if header.log_guid.int == 0 or not header.log_length:
        return 0
    if header.log_offset + header.log_length > file_size or header.log_length > 256 * MIB:
        return 0
    log = read(header.log_offset, header.log_length)
    ring = log + log     # entries may wrap around the end of the log
    want = header.log_guid.bytes_le
    count = 0
    for off in range(0, len(log), 4096):
        if log[off:off + 4] != b"loge" or log[off + 32:off + 48] != want:
            continue
        length, = struct.unpack_from("<I", log, off + 8)
        if length < 4096 or length % 4096 or length > len(log):
            continue
        if checksum_ok(ring[off:off + length]):
            count += 1
    return count


def parse_parent_locator(data: bytes) -> dict[str, str]:
    if len(data) < 20:
        return {}
    count, = struct.unpack_from("<H", data, 18)
    result: dict[str, str] = {}
    for n in range(count):
        e = 20 + 12 * n
        if e + 12 > len(data):
            break
        key_off, value_off, key_len, value_len = struct.unpack_from("<IIHH", data, e)
        key = bytes(data[key_off:key_off + key_len]).decode("utf-16-le", "replace")
        value = bytes(data[value_off:value_off + value_len]).decode("utf-16-le", "replace")
        result[key] = value
    return result


class VhdxLayer(Layer):
    format_label = "VHDX"

    def __init__(self, path):
        super().__init__(path)
        try:
            self.info = parse(lambda o, n: self.pread(n, o), self.file_size)
            info = self.info
            if info.dirty:
                raise UnsupportedImageError(
                    f"{self.path.name} was not closed cleanly (its log still has changes to apply). "
                    "Start and shut down the virtual machine once in Hyper-V, or repair the file "
                    "with VertConvert&Create, then try again.")
            self.size = info.virtual_size
            self.differencing = info.has_parent
            self.identity = "{" + str(info.header.data_write_guid).upper() + "}"
            if self.differencing:
                self.parent_identity = info.parent_linkage.upper() or None
                self.parent_hints = info.parent_paths
            entries = info.bat_entries
            if entries * 8 > info.bat_length:
                raise ImageFormatError("the VHDX block table is too small for its size")
            self.bat = array("Q")
            self.bat.frombytes(self.pread(entries * 8, info.bat_offset))
            if sys.byteorder == "big":
                self.bat.byteswap()
            self._check_bat()
            self._bitmaps = LRU(2)
            self._runs = LRU(256)
        except Exception:
            self.close()
            raise

    def _check_bat(self) -> None:
        info = self.info
        cr = info.chunk_ratio
        last_payload = last_bitmap = -1
        for i, entry in enumerate(self.bat):
            state = entry & 7
            if (i + 1) % (cr + 1) == 0:          # sector bitmap entry
                if state == SB_PRESENT:
                    last_bitmap = max(last_bitmap, entry >> 20)
            elif state in (FULLY_PRESENT, PARTIALLY_PRESENT):
                last_payload = max(last_payload, entry >> 20)
        if last_payload >= 0:
            self.check_extent(last_payload << 20, info.block_size)
        if last_bitmap >= 0:
            self.check_extent(last_bitmap << 20, MIB, "a sector bitmap")

    def _block_runs(self, block: int) -> list[tuple[int, int, bool]] | None:
        info = self.info
        cr = info.chunk_ratio
        chunk = block // cr
        sb_entry = self.bat[chunk * (cr + 1) + cr]
        if sb_entry & 7 != SB_PRESENT:
            return None
        per_block = info.block_size // info.logical_sector
        sb_offset = (sb_entry >> 20) << 20

        def load():
            bitmap = self._bitmaps.get_or(chunk, lambda: self.pread(MIB, sb_offset))
            return bit_runs(bitmap, (block % cr) * per_block, per_block, msb_first=False)

        return self._runs.get_or(block, load)

    def map(self, offset: int, length: int) -> Iterator[Extent]:
        info = self.info
        bs = info.block_size
        cr = info.chunk_ratio
        sector = info.logical_sector
        missing = PARENT if self.differencing else ZERO
        end = min(offset + length, self.size)
        while offset < end:
            block, within = divmod(offset, bs)
            n = min(end - offset, bs - within)
            entry = self.bat[block + block // cr]
            state = entry & 7
            file_offset = (entry >> 20) << 20
            if state == FULLY_PRESENT:
                yield DATA, n, file_offset + within
            elif state == BLOCK_ZERO:
                yield ZERO, n, 0
            elif state == PARTIALLY_PRESENT and self.differencing:
                runs = self._block_runs(block)
                if runs is None:
                    yield PARENT, n, 0
                else:
                    base = (block % cr) * (bs // sector)
                    pos = offset
                    for start, stop, present in runs:
                        a = max(pos, block * bs + (start - base) * sector)
                        b = min(offset + n, block * bs + (stop - base) * sector)
                        if b > a:
                            yield (DATA, b - a, file_offset + (a - block * bs)) if present else (PARENT, b - a, 0)
                            pos = b
            elif state in (NOT_PRESENT, UNDEFINED, UNMAPPED, UNMAPPED_V095, PARTIALLY_PRESENT):
                # not stored here: the parent's data (differencing) or zeros
                yield missing, n, 0
            else:
                raise ImageFormatError(f"{self.path.name}: invalid block state {state} in the block table")
            offset += n


def mark_regions_required(path) -> bool:
    """Set the "required" flag on the BAT and metadata regions, as [MS-VHDX] asks.

    qemu-img leaves the flag clear. Hyper-V doesn't mind, but VirtualBox refuses
    such files, so VertConvert&Create fixes the VHDX files it writes. Returns True if
    anything changed.
    """
    changed = False
    with open(path, "r+b") as f:
        for off in REGION_OFFSETS:
            f.seek(off)
            table = bytearray(f.read(64 * 1024))
            if table[:4] != b"regi" or not checksum_ok(table):
                continue
            count, = struct.unpack_from("<I", table, 8)
            dirty = False
            for n in range(min(count, 2047)):
                e = 16 + 32 * n
                if guid(table, e) in (BAT_REGION, METADATA_REGION):
                    flags, = struct.unpack_from("<I", table, e + 28)
                    if not flags & 1:
                        struct.pack_into("<I", table, e + 28, flags | 1)
                        dirty = True
            if dirty:
                struct.pack_into("<I", table, 4, 0)
                struct.pack_into("<I", table, 4, crc32c(bytes(table)))
                f.seek(off)
                f.write(table)
                changed = True
        if changed:
            f.flush()
            os.fsync(f.fileno())
    return changed
