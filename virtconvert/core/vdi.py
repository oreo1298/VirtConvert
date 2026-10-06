"""VDI (VirtualBox) headers and block map.

A VDI file starts with a text banner, a signature and a header (version 1.1),
followed by the block map: one little-endian 32-bit entry per block giving the
block's index in the data area, or "free" (in a snapshot: the parent's data)
or "zero". Snapshots are differencing images that name their parent by UUID
only; VirtualBox keeps the paths in the virtual machine's .vbox file.
"""

from __future__ import annotations

import struct
import sys
import uuid
from array import array
from dataclasses import dataclass
from typing import Iterator

from .errors import ImageFormatError, UnsupportedImageError
from .layers import DATA, PARENT, ZERO, Extent, Layer

SIGNATURE = 0xBEDA107F
VERSION_1_1 = 0x00010001
NORMAL, FIXED, UNDO, DIFF = 1, 2, 3, 4
TYPE_NAMES = {NORMAL: "dynamic", FIXED: "fixed", UNDO: "undo", DIFF: "differencing"}
BLOCK_FREE = 0xFFFFFFFF
BLOCK_ZERO = 0xFFFFFFFE


@dataclass
class VdiHeader:
    version: int
    image_type: int
    flags: int
    comment: str
    off_blocks: int
    off_data: int
    disk_size: int
    block_size: int
    block_extra: int
    blocks: int
    blocks_allocated: int
    uuid_create: str
    uuid_modify: str
    uuid_linkage: str
    uuid_parent_modify: str

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.image_type, f"type {self.image_type}")


def _uuid(data: bytes, offset: int) -> str:
    return str(uuid.UUID(bytes_le=bytes(data[offset:offset + 16])))


def is_vdi(head: bytes) -> bool:
    return len(head) >= 0x48 and struct.unpack_from("<I", head, 0x40)[0] == SIGNATURE


def parse_header(data: bytes) -> VdiHeader:
    if not is_vdi(data):
        raise ImageFormatError("not a VDI file")
    version, = struct.unpack_from("<I", data, 0x44)
    if version != VERSION_1_1:
        raise UnsupportedImageError(
            f"VDI version {version >> 16}.{version & 0xFFFF} is not supported (only 1.1, "
            "used by VirtualBox 2 and newer). Open the disk in VirtualBox's Virtual Media "
            "Manager and copy it to get a current VDI.")
    (_cb_header, image_type, flags) = struct.unpack_from("<III", data, 0x48)
    comment = bytes(data[0x54:0x154]).split(b"\0", 1)[0].decode("utf-8", "replace")
    off_blocks, off_data = struct.unpack_from("<II", data, 0x154)
    disk_size, = struct.unpack_from("<Q", data, 0x170)
    block_size, block_extra, blocks, allocated = struct.unpack_from("<IIII", data, 0x178)
    header = VdiHeader(
        version=version, image_type=image_type, flags=flags, comment=comment,
        off_blocks=off_blocks, off_data=off_data, disk_size=disk_size, block_size=block_size,
        block_extra=block_extra, blocks=blocks, blocks_allocated=allocated,
        uuid_create=_uuid(data, 0x188), uuid_modify=_uuid(data, 0x198),
        uuid_linkage=_uuid(data, 0x1A8), uuid_parent_modify=_uuid(data, 0x1B8))
    if block_size == 0 or block_size % 512:
        raise ImageFormatError(f"invalid VDI block size {block_size}")
    if blocks * block_size < disk_size:
        raise ImageFormatError("the VDI block map is too small for its disk size")
    return header


class VdiLayer(Layer):
    format_label = "VDI"

    def __init__(self, path):
        super().__init__(path)
        try:
            h = self.header = parse_header(self.pread(512, 0))
            if h.image_type not in (NORMAL, FIXED, DIFF):
                raise UnsupportedImageError(f"VDI images of type '{h.type_name}' are not supported")
            self.size = h.disk_size
            self.identity = h.uuid_create
            self.differencing = h.image_type == DIFF
            if self.differencing:
                self.parent_identity = h.uuid_linkage
            bmap = array("I")
            bmap.frombytes(self.pread(4 * h.blocks, h.off_blocks))
            if sys.byteorder == "big":
                bmap.byteswap()
            self.bmap = bmap
        except Exception:
            self.close()
            raise

    def map(self, offset: int, length: int) -> Iterator[Extent]:
        h = self.header
        bs = h.block_size
        missing = PARENT if self.differencing else ZERO
        end = min(offset + length, self.size)
        while offset < end:
            block, within = divmod(offset, bs)
            n = min(end - offset, bs - within)
            entry = self.bmap[block]
            if entry == BLOCK_FREE:
                yield missing, n, 0
            elif entry == BLOCK_ZERO:
                yield ZERO, n, 0
            else:
                yield DATA, n, h.off_data + entry * (bs + h.block_extra) + h.block_extra + within
            offset += n
