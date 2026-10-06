"""Identify a disk image and read its headers, without qemu-img.

qemu-img guesses formats from the first bytes of a file and so takes fixed VHD
files for raw images, and it reads differencing VHD files as if they had no
parent. VirtConvert therefore decides the format itself and always tells
qemu-img which one to use.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import formats as F
from . import vdi, vhd, vhdx
from .errors import ConvertError, ImageFormatError
from .formats import Format
from .partitions import PartitionTable
from .util import allocated_bytes, human_bytes

QCOW_MAGIC = b"QFI\xfb"
VMDK_SPARSE_MAGIC = b"KDMV"
VMDK_COWD_MAGIC = b"COWD"
VMDK_DESCRIPTOR = b"# Disk DescriptorFile"


@dataclass
class ImageInfo:
    path: Path
    format: Format | None = None
    file_size: int = 0
    allocated: int = 0
    virtual_size: int = 0
    subformat: str = ""
    disk_id: str = ""
    creator: str = ""
    block_size: int = 0
    logical_sector: int = 512
    physical_sector: int = 0
    differencing: bool = False
    identity: str | None = None
    parent_identity: str | None = None
    parent_identity_alt: list[str] = field(default_factory=list)
    parent_hints: list[str] = field(default_factory=list)
    backing_file: str | None = None       # parents qemu-img follows itself (QCOW2, VMDK)
    dirty: bool = False                   # VHDX log needs replaying
    encrypted: bool = False
    extent_files: list[Path] = field(default_factory=list)
    chain: list["ImageInfo"] = field(default_factory=list)   # parents, nearest first
    chain_size: int = 0                   # bytes stored in the whole chain
    partitions: PartitionTable | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and self.format is not None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def label(self) -> str:
        if self.format is None:
            return "Unknown"
        return f"{self.format.label} · {self.subformat}" if self.subformat else self.format.label

    @property
    def needs_chain_reader(self) -> bool:
        """Differencing VHD/VHDX/VDI: VirtConvert merges the chain itself."""
        return self.differencing and self.format in (F.VHD, F.VHDX, F.VDI)

    @property
    def stored_bytes(self) -> int:
        """Rough amount of data in the image (and its parents)."""
        return self.chain_size or self.allocated

    def describe(self) -> list[tuple[str, str]]:
        rows = [("Format", self.label), ("Disk size", f"{human_bytes(self.virtual_size)} "
                                                      f"({self.virtual_size:,} bytes)"),
                ("File size", f"{human_bytes(self.file_size)} ({human_bytes(self.allocated)} on disk)")]
        if self.block_size:
            rows.append(("Block size", human_bytes(self.block_size)))
        if self.format is F.VHDX:
            rows.append(("Sector size", f"{self.logical_sector} B logical, {self.physical_sector} B physical"))
        if self.disk_id:
            rows.append(("Disk ID", self.disk_id))
        if self.creator:
            rows.append(("Created by", self.creator))
        if self.partitions is not None:
            rows.append(("Partitions", self.partitions.label))
            if self.partitions.firmware:
                rows.append(("Boots with", self.partitions.boot_label))
        if self.chain:
            rows.append(("Based on", " → ".join(p.name for p in self.chain)))
        if self.backing_file:
            rows.append(("Backing file", self.backing_file))
        return rows


def detect(path: Path, head: bytes, file_size: int, read) -> Format | None:
    if head[:8] == vhdx.SIGNATURE:
        return F.VHDX
    if head[:8] == vhd.COOKIE:
        return F.VHD
    if vdi.is_vdi(head):
        return F.VDI
    if head[:4] in (VMDK_SPARSE_MAGIC, VMDK_COWD_MAGIC) or head.startswith(VMDK_DESCRIPTOR):
        return F.VMDK
    if head[:4] == QCOW_MAGIC:
        return F.QCOW2
    if file_size >= 512:
        for size in (512, 511):
            if read(file_size - size, 8) == vhd.COOKIE:
                return F.VHD
    if b"createType=" in head[:4096] and b"# Extent description" in head[:4096]:
        return F.VMDK
    guess = F.by_extension(str(path))
    if guess is F.RAW:
        return F.RAW
    return None


def probe(path: str | os.PathLike) -> ImageInfo:
    """Read the headers of PATH. Problems end up in info.errors, not exceptions."""
    path = Path(path)
    info = ImageInfo(path=path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        info.errors.append(f"Cannot open {path.name}: {exc.strerror or exc}")
        return info
    try:
        st = os.fstat(fd)
        if not path.is_file():
            info.errors.append(f"{path.name} is not a regular file")
            return info
        info.file_size = st.st_size
        info.allocated = allocated_bytes(path)

        def read(offset: int, length: int) -> bytes:
            data = os.pread(fd, length, offset)
            return data + bytes(length - len(data)) if len(data) < length else data

        head = read(0, 64 * 1024)
        fmt = detect(path, head, info.file_size, read)
        info.format = fmt
        if fmt is None:
            info.errors.append(f"{path.name} is not a disk image VirtConvert recognises "
                               "(VHD, VHDX, VDI, VMDK, QCOW2 or a raw .img).")
            return info
        try:
            {F.VHD: _vhd, F.VHDX: _vhdx, F.VDI: _vdi, F.VMDK: _vmdk, F.QCOW2: _qcow2,
             F.RAW: _raw}[fmt](info, head, read)
        except ConvertError as exc:
            info.errors.append(f"{path.name}: {exc}")
        except (struct.error, ValueError, IndexError) as exc:
            info.errors.append(f"{path.name}: the {fmt.label} headers are damaged ({exc})")
        expected = F.by_extension(str(path))
        if expected is not None and expected is not fmt and expected is not F.RAW:
            info.notes.append(f"The file name says {expected.label}, but the file is a {fmt.label} image.")
        if fmt.max_size and info.virtual_size > fmt.max_size:
            info.warnings.append(f"The disk is larger than {fmt.label} allows ({fmt.max_label}).")
        return info
    except OSError as exc:
        info.errors.append(f"Cannot read {path.name}: {exc.strerror or exc}")
        return info
    finally:
        os.close(fd)


def _vhd(info: ImageInfo, head: bytes, read) -> None:
    footer, footer_offset = vhd.locate_footer(read, info.file_size)
    info.virtual_size = footer.virtual_size
    info.subformat = footer.type_name
    info.disk_id = footer.unique_id
    info.identity = footer.unique_id
    info.creator = footer.creator
    if footer_offset < 0:
        info.warnings.append("The footer at the end of the file is missing (the file may be cut off); "
                             "using the copy at the start.")
    if not footer.checksum_ok:
        info.warnings.append("The VHD footer checksum is wrong; the file may be damaged.")
    if footer.saved_state:
        info.notes.append("The disk was saved with the VM in a saved state; shut the VM down "
                          "properly for a consistent copy.")
    if footer.creator_app == "vpc " and 0 < footer.chs_size < footer.current_size:
        info.notes.append(f"Virtual PC showed this disk as {footer.chs_size:,} bytes (its CHS "
                          f"geometry); the converted disk keeps all {footer.current_size:,} bytes.")
    if footer.disk_type not in (vhd.FIXED, vhd.DYNAMIC, vhd.DIFFERENCING):
        info.errors.append(f"Unknown VHD disk type {footer.disk_type}.")
        return
    if footer.disk_type == vhd.FIXED:
        if info.file_size - 512 < footer.virtual_size:
            info.errors.append("The fixed VHD is shorter than its disk size (the file is cut off).")
        return
    header = vhd.parse_dynamic_header(read(footer.data_offset, 1024), read)
    info.block_size = header.block_size
    if not header.checksum_ok:
        info.warnings.append("The VHD dynamic header checksum is wrong; the file may be damaged.")
    if footer.disk_type == vhd.DIFFERENCING:
        info.differencing = True
        info.parent_identity = header.parent_id
        hints = [p for _c, p in header.locators]
        if header.parent_name:
            hints.append(header.parent_name)
        info.parent_hints = list(dict.fromkeys(hints))


def _vhdx(info: ImageInfo, head: bytes, read) -> None:
    v = vhdx.parse(read, info.file_size)
    info.virtual_size = v.virtual_size
    info.subformat = v.subformat
    info.disk_id = v.disk_id
    info.creator = v.creator
    info.block_size = v.block_size
    info.logical_sector = v.logical_sector
    info.physical_sector = v.physical_sector
    info.identity = "{" + str(v.header.data_write_guid).upper() + "}"
    info.dirty = v.dirty
    if v.damaged_copy:
        info.warnings.append(f"The VHDX {', '.join(v.damaged_copy)} is damaged; the other copy is used.")
    if v.dirty:
        info.errors.append("The VHDX was not closed cleanly: its log still holds changes that must "
                           "be applied first (the VM was running, or Windows crashed). Shut the VM "
                           "down in Hyper-V, or use Repair, which applies the log to the file.")
    if v.has_parent:
        info.differencing = True
        info.parent_identity = v.parent_linkage.upper() or None
        info.parent_hints = v.parent_paths
        if v.parent_locator.get("parent_linkage2"):
            info.parent_identity_alt.append(v.parent_locator["parent_linkage2"])


def _vdi(info: ImageInfo, head: bytes, read) -> None:
    h = vdi.parse_header(head)
    info.virtual_size = h.disk_size
    info.subformat = h.type_name
    info.disk_id = h.uuid_create
    info.identity = h.uuid_create
    info.block_size = h.block_size
    if h.comment:
        info.notes.append(f"Comment: {h.comment}")
    if h.image_type == vdi.DIFF:
        info.differencing = True
        info.parent_identity = h.uuid_linkage
    elif h.image_type not in (vdi.NORMAL, vdi.FIXED):
        info.errors.append(f"VDI images of type '{h.type_name}' are not supported.")


_DESC_KV = re.compile(rb'^\s*([A-Za-z0-9_.]+)\s*=\s*"?([^"\r\n]*)"?', re.M)
_DESC_EXTENT = re.compile(rb'^\s*(RW|RDONLY|NOACCESS)\s+(\d+)\s+(\w+)\s*(?:"([^"]*)")?', re.M)


def _vmdk(info: ImageInfo, head: bytes, read) -> None:
    descriptor = b""
    if head[:4] == VMDK_SPARSE_MAGIC:
        (_version, _flags, capacity, grain, desc_off, desc_size) = struct.unpack_from("<IIQQQQ", head, 4)
        info.virtual_size = capacity * 512
        info.block_size = grain * 512
        if desc_off and desc_size:
            descriptor = read(desc_off * 512, min(desc_size * 512, 1 << 20))
        info.subformat = "monolithicSparse"
        compress, = struct.unpack_from("<H", head, 77)
        if compress:
            info.subformat = "streamOptimized"
    elif head[:4] == VMDK_COWD_MAGIC:
        _version, _flags, sectors = struct.unpack_from("<III", head, 4)
        info.virtual_size = sectors * 512
        info.subformat = "ESX COW"
    else:
        descriptor = head
    if descriptor:
        values = {k.decode().lower(): v.decode("utf-8", "replace") for k, v in _DESC_KV.findall(descriptor)}
        if values.get("createtype"):
            info.subformat = values["createtype"]
        parent_cid = values.get("parentcid", "ffffffff").lower()
        hint = values.get("parentfilenamehint")
        if parent_cid != "ffffffff" and hint:
            info.backing_file = hint
            info.notes.append(f"This is a snapshot of {Path(hint.replace(chr(92), '/')).name}; "
                              "qemu-img merges it with its parent.")
        info.disk_id = values.get("ddb.uuid.image", "")
        if values.get("ddb.toolsversion") or values.get("ddb.virtualhwversion"):
            info.creator = "VMware"
        extents = _DESC_EXTENT.findall(descriptor)
        if head[:4] not in (VMDK_SPARSE_MAGIC, VMDK_COWD_MAGIC):
            total = 0
            for _access, sectors, kind, name in extents:
                total += int(sectors)
                if name and kind.upper() != b"ZERO":
                    f = info.path.parent / name.decode("utf-8", "replace")
                    info.extent_files.append(f)
                    if not f.exists():
                        info.errors.append(f"The VMDK's data file {f.name} is missing.")
            info.virtual_size = total * 512
            info.allocated = sum(allocated_bytes(f) for f in info.extent_files) or info.allocated
    if not info.virtual_size and not info.errors:
        raise ImageFormatError("the VMDK has no size")


def _qcow2(info: ImageInfo, head: bytes, read) -> None:
    version, backing_off, backing_len, cluster_bits, size, crypt = struct.unpack_from(">IQIIQI", head, 4)
    info.virtual_size = size
    info.subformat = f"version {version}"
    if version not in (2, 3):
        info.errors.append(f"QCOW version {version} is not supported.")
    if 9 <= cluster_bits <= 21:
        info.block_size = 1 << cluster_bits
    if crypt:
        info.encrypted = True
        info.errors.append("The QCOW2 image is encrypted; decrypt it with qemu-img first.")
    if backing_off and 0 < backing_len < 1024:
        info.backing_file = read(backing_off, backing_len).decode("utf-8", "replace")
        info.notes.append(f"Based on {info.backing_file}; qemu-img merges it with its parent.")
    if version >= 3 and len(head) > 80:
        incompatible, = struct.unpack_from(">Q", head, 72)
        if incompatible & 1:
            info.warnings.append("The QCOW2 image was not closed cleanly (dirty); qemu-img repairs "
                                 "this in memory while reading.")


def _raw(info: ImageInfo, head: bytes, read) -> None:
    info.virtual_size = info.file_size
    info.subformat = ""
    if info.file_size % 512:
        info.warnings.append("The size is not a whole number of 512-byte sectors.")
