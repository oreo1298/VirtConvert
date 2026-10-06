"""VHD (Virtual PC / Hyper-V generation 1 / Azure) headers and block map.

Layout (big-endian): a 512-byte footer at the end of the file; fixed disks
are raw data followed by the footer. Dynamic and differencing disks start
with a copy of the footer, then a 1024-byte dynamic header pointing at the
block allocation table (BAT). Each allocated block is a sector bitmap
(most significant bit first) followed by the block's data.
"""

from __future__ import annotations

import struct
import sys
import uuid
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .errors import ImageFormatError
from .layers import DATA, LRU, PARENT, ZERO, Extent, Layer, bit_runs
from .util import round_up

COOKIE = b"conectix"
SPARSE_COOKIE = b"cxsparse"
FIXED, DYNAMIC, DIFFERENCING = 2, 3, 4
TYPE_NAMES = {FIXED: "fixed", DYNAMIC: "dynamic", DIFFERENCING: "differencing"}
UNUSED = 0xFFFFFFFF
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
CREATORS = {
    "win ": "Hyper-V / Windows", "d2v ": "Disk2vhd", "vpc ": "Virtual PC", "vs  ": "Virtual Server",
    "qemu": "QEMU", "qem2": "QEMU", "vbox": "VirtualBox", "tap\0": "XenServer", "CTXS": "XenConverter",
    "wa\0\0": "Azure", "MSFT": "Microsoft",
}
# parent locator platform codes: relative/absolute Windows paths (UTF-16LE), Mac URLs (UTF-8)
LOCATOR_CODES = {b"W2ru": "utf-16-le", b"W2ku": "utf-16-le", b"Wi2r": "latin-1", b"Wi2k": "latin-1",
                 b"MacX": "utf-8", b"Mac ": "utf-8"}


def _guid(data: bytes, offset: int) -> str:
    """IDs are Windows GUIDs (Hyper-V and VirtualBox show them this way)."""
    return str(uuid.UUID(bytes_le=bytes(data[offset:offset + 16])))


def checksum(data: bytes, field_offset: int) -> int:
    total = sum(data[:field_offset]) + sum(data[field_offset + 4:])
    return (~total) & 0xFFFFFFFF


@dataclass
class Footer:
    features: int
    version: int
    data_offset: int
    timestamp: int
    creator_app: str
    creator_version: int
    creator_os: str
    original_size: int
    current_size: int
    cylinders: int
    heads: int
    sectors: int
    disk_type: int
    checksum_ok: bool
    unique_id: str
    saved_state: bool

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.disk_type, f"type {self.disk_type}")

    @property
    def creator(self) -> str:
        name = CREATORS.get(self.creator_app, self.creator_app.strip("\0 ") or "unknown")
        major, minor = self.creator_version >> 16, self.creator_version & 0xFFFF
        return f"{name} {major}.{minor}" if self.creator_version else name

    @property
    def created(self) -> datetime:
        return EPOCH + timedelta(seconds=self.timestamp)

    @property
    def chs_size(self) -> int:
        return self.cylinders * self.heads * self.sectors * 512

    @property
    def virtual_size(self) -> int:
        """The disk size, as Hyper-V and VirtualBox see it.

        Virtual PC showed disks at their CHS geometry size, which is a few KB
        smaller; qemu-img does the same for files from unknown programs (and
        would cut off the end of VirtualBox-made VHDs). VirtConvert always uses
        the full size and tells qemu-img to do so (force_size_calc).
        """
        return self.current_size


def parse_footer(data: bytes) -> Footer:
    if len(data) < 512 or data[:8] != COOKIE:
        raise ImageFormatError("not a VHD footer")
    (features, version, data_offset, timestamp, creator_app, creator_version, creator_os,
     original_size, current_size, cyl, heads, secs, disk_type, csum) = struct.unpack_from(
        ">IIQI4sI4sQQHBBII", data, 8)
    return Footer(
        features=features, version=version, data_offset=data_offset, timestamp=timestamp,
        creator_app=creator_app.decode("latin-1"), creator_version=creator_version,
        creator_os=creator_os.decode("latin-1").strip("\0"), original_size=original_size,
        current_size=current_size, cylinders=cyl, heads=heads, sectors=secs, disk_type=disk_type,
        checksum_ok=checksum(data[:512], 64) == csum,
        unique_id=_guid(data, 68), saved_state=bool(data[84]))


@dataclass
class DynamicHeader:
    table_offset: int
    max_entries: int
    block_size: int
    checksum_ok: bool
    parent_id: str
    parent_timestamp: int
    parent_name: str
    locators: list[tuple[str, str]] = field(default_factory=list)  # (code, path)

    @property
    def bitmap_size(self) -> int:
        """One bit per sector, padded to whole sectors."""
        return round_up(round_up(self.block_size // 512, 8) // 8, 512)


def parse_dynamic_header(data: bytes, read) -> DynamicHeader:
    """READ(offset, length) fetches the parent locator data from the file."""
    if len(data) < 1024 or data[:8] != SPARSE_COOKIE:
        raise ImageFormatError("the VHD's dynamic disk header is missing or damaged")
    table_offset, _version, max_entries, block_size, csum = struct.unpack_from(">QIIII", data, 16)
    parent_id = _guid(data, 40)
    parent_ts = struct.unpack_from(">I", data, 56)[0]
    parent_name = bytes(data[64:576]).decode("utf-16-be", "replace").split("\0", 1)[0]
    locators: list[tuple[str, str]] = []
    for i in range(8):
        code, _space, length, _res, offset = struct.unpack_from(">4sIIIQ", data, 576 + 24 * i)
        if code in LOCATOR_CODES and 0 < length <= 65536 and offset:
            raw = read(offset, length)
            text = raw.decode(LOCATOR_CODES[code], "replace").split("\0", 1)[0]
            if text:
                locators.append((code.decode("latin-1"), text))
    if block_size == 0 or block_size & (block_size - 1) or block_size % 512:
        raise ImageFormatError(f"the VHD's block size ({block_size}) is invalid")
    return DynamicHeader(table_offset=table_offset, max_entries=max_entries, block_size=block_size,
                         checksum_ok=checksum(data[:1024], 36) == csum, parent_id=parent_id,
                         parent_timestamp=parent_ts, parent_name=parent_name, locators=locators)


def locate_footer(read, file_size: int) -> tuple[Footer, int]:
    """Find the footer: at the end of the file (also as an old 511-byte footer),
    or the copy at the start of dynamic disks if the end was cut off."""
    for size in (512, 511):
        if file_size >= size:
            data = read(file_size - size, size)
            if data[:8] == COOKIE:
                return parse_footer(data + bytes(512 - size)), file_size - size
    head = read(0, 512)
    if head[:8] == COOKIE:
        return parse_footer(head), -1
    raise ImageFormatError("not a VHD file (no footer)")


class VhdLayer(Layer):
    format_label = "VHD"

    def __init__(self, path):
        super().__init__(path)
        try:
            self.footer, self.footer_offset = locate_footer(lambda o, n: self.pread(n, o),
                                                            self.file_size)
            f = self.footer
            if f.disk_type not in (FIXED, DYNAMIC, DIFFERENCING):
                raise ImageFormatError(f"unknown VHD disk type {f.disk_type}")
            self.size = f.virtual_size
            self.identity = f.unique_id
            self.differencing = f.disk_type == DIFFERENCING
            self.header: DynamicHeader | None = None
            self.bat: array | None = None
            if f.disk_type != FIXED:
                if f.data_offset in (UNUSED << 32 | UNUSED, 0) or f.data_offset >= self.file_size:
                    raise ImageFormatError("the VHD's dynamic disk header offset is invalid")
                self.header = parse_dynamic_header(self.pread(1024, f.data_offset),
                                                   lambda o, n: self.pread(n, o))
                h = self.header
                needed = -(-self.size // h.block_size)
                if h.max_entries < needed:
                    raise ImageFormatError("the VHD's block table is too small for its size")
                bat = array("I")
                bat.frombytes(self.pread(4 * needed, h.table_offset))
                if sys.byteorder == "little":
                    bat.byteswap()
                self.bat = bat
                used = [e for e in bat if e != UNUSED]
                if used:
                    self.check_extent(max(used) * 512, h.bitmap_size + h.block_size)
                if self.differencing:
                    self.parent_identity = h.parent_id
                    hints = [path for _code, path in h.locators]
                    if h.parent_name:
                        hints.append(h.parent_name)
                    self.parent_hints = list(dict.fromkeys(hints))
            elif self.size > max(self.file_size - 512, 0):
                raise ImageFormatError("the fixed VHD is shorter than its disk size (truncated?)")
            self._runs = LRU(256)
        except Exception:
            self.close()
            raise

    def _bitmap_runs(self, block: int, sector_offset: int) -> list[tuple[int, int, bool]]:
        h = self.header
        sectors = h.block_size // 512

        def load():
            bitmap = self.pread(h.bitmap_size, sector_offset)
            return bit_runs(bitmap, 0, sectors, msb_first=True)

        return self._runs.get_or(block, load)

    def map(self, offset: int, length: int) -> Iterator[Extent]:
        f = self.footer
        end = min(offset + length, self.size)
        if f.disk_type == FIXED:
            if end > offset:
                yield DATA, end - offset, offset
            return
        h = self.header
        bs = h.block_size
        missing = PARENT if self.differencing else ZERO
        while offset < end:
            block, within = divmod(offset, bs)
            n = min(end - offset, bs - within)
            entry = self.bat[block]
            if entry == UNUSED:
                yield missing, n, 0
            else:
                base = entry * 512
                data = base + h.bitmap_size
                if not self.differencing:
                    # like qemu and Hyper-V: an allocated block of a dynamic disk is all data
                    yield DATA, n, data + within
                else:
                    first = within // 512
                    last = (within + n - 1) // 512
                    pos = offset
                    for start, stop, present in self._bitmap_runs(block, base):
                        if stop <= first or start > last:
                            continue
                        a = max(pos, block * bs + start * 512)
                        b = min(offset + n, block * bs + stop * 512)
                        if b > a:
                            yield (DATA, b - a, data + (a - block * bs)) if present else (PARENT, b - a, 0)
                            pos = b
            offset += n
