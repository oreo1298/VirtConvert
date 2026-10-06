"""Write small VHD, VHDX, VDI and DMG test images, including differencing ones.

Independent of the readers in vertconvert.core: written from the format
specifications, so the tests check the readers against the specs (and against
VirtualBox and libvhdi where those are installed).

An image's content is a dict {sector number: 512 bytes} (or {block: bytes} for
VDI) of what that file stores; everything else is zeros (base images) or comes
from the parent (differencing images). Sectors a differencing image does not
store are filled with 0xEE in the file, so a reader that ignores the sector
bitmaps returns visibly wrong data.
"""

from __future__ import annotations

import base64
import struct
import time
import uuid
import zlib
from pathlib import Path

SECTOR = 512
MIB = 1 << 20
GARBAGE = 0xEE


def round_up(v: int, m: int) -> int:
    return -(-v // m) * m


# ---------------------------------------------------------------------- partition tables

def gpt_sectors(disk_size: int, partitions: list[tuple[int, int, str, str]],
                sector: int = SECTOR) -> dict[int, bytes]:
    """Protective MBR, primary GPT and backup GPT as {byte offset: bytes}.

    PARTITIONS: (first LBA, last LBA, type GUID, name)."""
    lbas = disk_size // sector
    disk_guid = uuid.uuid4()
    entries = bytearray(128 * 128)
    for i, (first, last, type_guid, name) in enumerate(partitions):
        e = i * 128
        entries[e:e + 16] = uuid.UUID(type_guid).bytes_le
        entries[e + 16:e + 32] = uuid.uuid4().bytes_le
        struct.pack_into("<QQQ", entries, e + 32, first, last, 0)
        entries[e + 56:e + 128] = name.encode("utf-16-le")[:72].ljust(72, b"\0")
    entries_crc = zlib.crc32(entries)
    entry_lbas = len(entries) // sector

    def header(current: int, backup: int, entries_lba: int) -> bytes:
        h = bytearray(92)
        h[0:8] = b"EFI PART"
        struct.pack_into("<IIIIQQQQ", h, 8, 0x00010000, 92, 0, 0, current, backup,
                         2 + entry_lbas - 1 + 1, lbas - 2 - entry_lbas)
        h[56:72] = disk_guid.bytes_le
        struct.pack_into("<QIII", h, 72, entries_lba, 128, 128, entries_crc)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)))
        return bytes(h).ljust(sector, b"\0")

    mbr = bytearray(SECTOR)
    struct.pack_into("<B3sB3sII", mbr, 446, 0, b"\x00\x02\x00", 0xEE, b"\xff\xff\xff", 1,
                     min(lbas - 1, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"
    return {
        0: bytes(mbr),
        sector: header(1, lbas - 1, 2),
        2 * sector: bytes(entries),
        (lbas - 1 - entry_lbas) * sector: bytes(entries),
        (lbas - 1) * sector: header(lbas - 1, 1, lbas - 1 - entry_lbas),
    }


def mbr_sector(partitions: list[tuple[int, int, int, bool]], boot_code: bool = True) -> dict[int, bytes]:
    """An MBR. PARTITIONS: (first LBA, sectors, type, bootable)."""
    mbr = bytearray(SECTOR)
    if boot_code:
        mbr[0:3] = b"\xeb\x63\x90"
    for i, (first, count, ptype, boot) in enumerate(partitions):
        struct.pack_into("<B3sB3sII", mbr, 446 + 16 * i, 0x80 if boot else 0, b"\xfe\xff\xff", ptype,
                         b"\xfe\xff\xff", first, count)
    mbr[510:512] = b"\x55\xaa"
    return {0: bytes(mbr)}


def to_sectors(chunks: dict[int, bytes]) -> dict[int, bytes]:
    """{byte offset: data} (sector aligned) -> {sector: 512 bytes}."""
    out: dict[int, bytes] = {}
    for offset, data in chunks.items():
        assert offset % SECTOR == 0
        data = data.ljust(round_up(len(data), SECTOR), b"\0")
        for i in range(0, len(data), SECTOR):
            out[offset // SECTOR + i // SECTOR] = data[i:i + SECTOR]
    return out


def flatten(size: int, *layers: dict[int, bytes]) -> bytes:
    """What the disk reads as: LAYERS from the base to the newest."""
    buf = bytearray(size)
    for layer in layers:
        for s, data in layer.items():
            if s * SECTOR < size:
                buf[s * SECTOR:s * SECTOR + SECTOR] = data
    return bytes(buf)


# ---------------------------------------------------------------------- VHD

def _chs(size: int) -> tuple[int, int, int]:
    total = min(size // SECTOR, 65535 * 16 * 255)
    if total >= 65535 * 16 * 63:
        spt, heads = 255, 16
        cth = total // spt
    else:
        spt = 17
        cth = total // spt
        heads = max((cth + 1023) // 1024, 4)
        if cth >= heads * 1024 or heads > 16:
            spt, heads = 31, 16
            cth = total // spt
        if cth >= heads * 1024:
            spt, heads = 63, 16
            cth = total // spt
    return cth // heads, heads, spt


def _vhd_checksum(data: bytearray, offset: int) -> None:
    struct.pack_into(">I", data, offset, 0)
    struct.pack_into(">I", data, offset, (~sum(data)) & 0xFFFFFFFF)


def vhd_footer(size: int, disk_type: int, unique: uuid.UUID, data_offset: int,
               creator: bytes = b"win ") -> bytes:
    f = bytearray(512)
    cyl, heads, spt = _chs(size)
    stamp = int(time.time()) - 946684800
    struct.pack_into(">8sIIQI4sI4sQQHBBI", f, 0, b"conectix", 2, 0x00010000, data_offset, stamp,
                     creator, 0x000A0000, b"Wi2k", size, size, cyl, heads, spt, disk_type)
    f[68:84] = unique.bytes_le
    _vhd_checksum(f, 64)
    return bytes(f)


def write_vhd(path: Path, size: int, content: dict[int, bytes], kind: str = "dynamic",
              block_size: int = 2 * MIB, parent: Path | None = None, parent_id: uuid.UUID | None = None,
              unique: uuid.UUID | None = None, creator: bytes = b"win ") -> uuid.UUID:
    unique = unique or uuid.uuid4()
    if kind == "fixed":
        data = bytearray(flatten(size, content))
        path.write_bytes(bytes(data) + vhd_footer(size, 2, unique, 0xFFFFFFFFFFFFFFFF, creator))
        return unique
    diff = kind == "differencing"
    blocks = -(-size // block_size)
    sectors_per_block = block_size // SECTOR
    bitmap_size = round_up(sectors_per_block // 8, SECTOR)
    bat_offset = 1536
    bat_size = round_up(blocks * 4, SECTOR)
    pos = bat_offset + bat_size
    header = bytearray(1024)
    locators: list[tuple[bytes, bytes]] = []
    if diff:
        assert parent is not None and parent_id is not None
        rel = ".\\" + parent.name
        locators = [(b"W2ru", rel.encode("utf-16-le")),
                    (b"W2ku", ("C:\\VMs\\" + parent.name).encode("utf-16-le"))]
    locator_data = bytearray()
    entries = []
    for code, blob in locators:
        space = round_up(len(blob), SECTOR)
        entries.append((code, space // SECTOR, len(blob), pos + len(locator_data)))
        locator_data += blob.ljust(space, b"\0")
    pos += len(locator_data)
    struct.pack_into(">8sQQIII", header, 0, b"cxsparse", 0xFFFFFFFFFFFFFFFF, bat_offset, 0x00010000,
                     blocks, block_size)
    if diff:
        header[40:56] = parent_id.bytes_le
        struct.pack_into(">I", header, 56, int(time.time()) - 946684800)
        header[64:576] = parent.name.encode("utf-16-be")[:512].ljust(512, b"\0")
        for i, (code, space, length, offset) in enumerate(entries):
            struct.pack_into(">4sIIIQ", header, 576 + 24 * i, code, space, length, 0, offset)
    _vhd_checksum(header, 36)

    bat = [0xFFFFFFFF] * blocks
    block_data = bytearray()
    by_block: dict[int, dict[int, bytes]] = {}
    for s, d in content.items():
        by_block.setdefault(s // sectors_per_block, {})[s % sectors_per_block] = d
    pos = round_up(pos, SECTOR)
    for b in sorted(by_block):
        present = by_block[b]
        bitmap = bytearray(bitmap_size)
        data = bytearray((bytes([GARBAGE]) if diff else b"\0") * block_size)
        for i in range(sectors_per_block):
            if not diff or i in present:
                bitmap[i // 8] |= 0x80 >> (i % 8)
            if i in present:
                data[i * SECTOR:(i + 1) * SECTOR] = present[i]
        bat[b] = (pos + len(block_data)) // SECTOR
        block_data += bitmap + data
    footer = vhd_footer(size, 4 if diff else 3, unique, 512, creator)
    with open(path, "wb") as f:
        f.write(footer)
        f.write(header)
        f.write(struct.pack(f">{blocks}I", *bat).ljust(bat_size, b"\xff"))
        f.write(locator_data)
        f.seek(round_up(f.tell(), SECTOR))
        f.write(block_data)
        f.write(footer)
    return unique


# ---------------------------------------------------------------------- VHDX

_CRC_TABLE: list[int] = []


def crc32c(data: bytes) -> int:
    if not _CRC_TABLE:
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
            _CRC_TABLE.append(c)
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def _with_crc(block: bytearray) -> bytes:
    struct.pack_into("<I", block, 4, 0)
    struct.pack_into("<I", block, 4, crc32c(bytes(block)))
    return bytes(block)


BAT_GUID = uuid.UUID("2DC27766-F623-4200-9D64-115E9BFD4A08")
META_GUID = uuid.UUID("8B7CA206-4790-4B9A-B8FE-575F050F886E")
FILE_PARAMS = uuid.UUID("CAA16737-FA36-4D43-B3B6-33F0AA44E76B")
DISK_SIZE = uuid.UUID("2FA54224-CD1B-4876-B211-5DBED83BF4B8")
DISK_ID = uuid.UUID("BECA12AB-B2E6-4523-93EF-C309E000C746")
LOGICAL = uuid.UUID("8141BF1D-A96F-4709-BA47-F233A8FAAB5F")
PHYSICAL = uuid.UUID("CDA348C7-445D-4471-9CC9-E9885251C556")
PARENT_LOC = uuid.UUID("A8D35F2D-B30B-454D-ABF7-D3D84834AB0C")
VHDX_LOCATOR = uuid.UUID("B04AEFB7-D19E-4A81-B789-25B8E9445913")


def write_vhdx(path: Path, size: int, content: dict[int, bytes], kind: str = "dynamic",
               block_size: int = 2 * MIB, logical: int = SECTOR, parent: Path | None = None,
               parent_data_guid: uuid.UUID | None = None, zero_blocks: tuple[int, ...] = (),
               dirty: bool = False, stale_log: bool = False, required_flags: bool = True,
               creator: str = "VertConvert&Create test writer") -> uuid.UUID:
    """Returns the DataWriteGuid (what a child's parent_linkage refers to).

    CONTENT is keyed by logical sector, each value LOGICAL bytes."""
    diff = kind == "differencing"
    data_write = uuid.uuid4()
    log_guid = uuid.uuid4() if dirty or stale_log else uuid.UUID(int=0)
    blocks = -(-size // block_size)
    chunk_ratio = (1 << 23) * logical // block_size
    sb_blocks = -(-blocks // chunk_ratio)
    entries = sb_blocks * (chunk_ratio + 1) if diff else blocks + (blocks - 1) // chunk_ratio
    log_off, meta_off, bat_off = MIB, 2 * MIB, 3 * MIB
    bat_len = round_up(entries * 8, MIB)
    pos = bat_off + bat_len
    per_block = block_size // logical

    bat = [0] * entries
    payload = bytearray()
    by_block: dict[int, dict[int, bytes]] = {}
    for s, d in content.items():
        by_block.setdefault(s // per_block, {})[s % per_block] = d
    bitmaps: dict[int, bytearray] = {}
    for b in range(blocks):
        idx = b + b // chunk_ratio
        if b in zero_blocks:
            bat[idx] = 2
            continue
        present = by_block.get(b)
        if kind == "fixed" or present:
            present = present or {}
            full = len(present) == per_block
            data = bytearray((bytes([GARBAGE]) if diff else b"\0") * block_size)
            for i, d in present.items():
                data[i * logical:(i + 1) * logical] = d
            offset = pos + len(payload)
            payload += data
            if diff and not full:
                bat[idx] = 7 | (offset // MIB) << 20
                bm = bitmaps.setdefault(b // chunk_ratio, bytearray(MIB))
                base = (b % chunk_ratio) * per_block
                for i in present:
                    bit = base + i
                    bm[bit // 8] |= 1 << (bit % 8)
            else:
                bat[idx] = 6 | (offset // MIB) << 20
    for chunk, bm in bitmaps.items():
        offset = pos + len(payload)
        payload += bm
        bat[chunk * (chunk_ratio + 1) + chunk_ratio] = 6 | (offset // MIB) << 20

    ident = bytearray(64 * 1024)
    ident[0:8] = b"vhdxfile"
    ident[8:8 + 512] = creator.encode("utf-16-le")[:510].ljust(512, b"\0")

    def header(seq: int) -> bytes:
        h = bytearray(4096)
        h[0:4] = b"head"
        struct.pack_into("<Q", h, 8, seq)
        h[16:32] = uuid.uuid4().bytes_le
        h[32:48] = data_write.bytes_le
        h[48:64] = log_guid.bytes_le
        struct.pack_into("<HHIQ", h, 64, 0, 1, MIB, log_off)
        return _with_crc(h)

    region = bytearray(64 * 1024)
    region[0:4] = b"regi"
    struct.pack_into("<I", region, 8, 2)
    flag = 1 if required_flags else 0
    region[16:32] = BAT_GUID.bytes_le
    struct.pack_into("<QII", region, 32, bat_off, bat_len, flag)
    region[48:64] = META_GUID.bytes_le
    struct.pack_into("<QII", region, 64, meta_off, MIB, flag)
    region_bytes = _with_crc(region)

    meta = bytearray(MIB)
    meta[0:8] = b"metadata"
    items: list[tuple[uuid.UUID, bytes, int]] = [
        (FILE_PARAMS, struct.pack("<II", block_size, (1 if kind == "fixed" else 0) | (2 if diff else 0)), 4),
        (DISK_SIZE, struct.pack("<Q", size), 6),
        (DISK_ID, uuid.uuid4().bytes_le, 6),
        (LOGICAL, struct.pack("<I", logical), 6),
        (PHYSICAL, struct.pack("<I", 4096), 6),
    ]
    if diff:
        assert parent is not None and parent_data_guid is not None
        kv = [("parent_linkage", "{" + str(parent_data_guid) + "}"),
              ("relative_path", ".\\" + parent.name),
              ("absolute_win32_path", "D:\\Hyper-V\\Virtual Hard Disks\\" + parent.name)]
        loc = bytearray(20 + 12 * len(kv))
        loc[0:16] = VHDX_LOCATOR.bytes_le
        struct.pack_into("<HH", loc, 16, 0, len(kv))
        strings = bytearray()
        for n, (k, v) in enumerate(kv):
            kb, vb = k.encode("utf-16-le"), v.encode("utf-16-le")
            ko = len(loc) + len(strings)
            strings += kb
            vo = len(loc) + len(strings)
            strings += vb
            struct.pack_into("<IIHH", loc, 20 + 12 * n, ko, vo, len(kb), len(vb))
        items.append((PARENT_LOC, bytes(loc + strings), 4))
    struct.pack_into("<H", meta, 10, len(items))
    item_off = 64 * 1024
    for n, (gid, blob, flags) in enumerate(items):
        e = 32 + 32 * n
        meta[e:e + 16] = gid.bytes_le
        struct.pack_into("<III", meta, e + 16, item_off, len(blob), flags)
        meta[item_off:item_off + len(blob)] = blob
        item_off += round_up(len(blob), 8)

    with open(path, "wb") as f:
        f.write(ident)
        f.seek(64 * 1024)
        f.write(header(1))
        f.seek(128 * 1024)
        f.write(header(2))
        f.seek(192 * 1024)
        f.write(region_bytes)
        f.seek(256 * 1024)
        f.write(region_bytes)
        f.seek(log_off)
        log = bytearray(MIB)
        if dirty:
            # one valid entry without descriptors: a log that still has to be "replayed"
            end = round_up(pos + len(payload), MIB)
            struct.pack_into("<4sIIIQII", log, 0, b"loge", 0, 4096, 0, 1, 0, 0)
            log[32:48] = log_guid.bytes_le
            struct.pack_into("<QQ", log, 48, end, end)
            entry = bytearray(log[:4096])
            log[:4096] = _with_crc(entry)
        f.write(log)
        f.seek(meta_off)
        f.write(meta)
        f.seek(bat_off)
        f.write(struct.pack(f"<{entries}Q", *bat).ljust(bat_len, b"\0"))
        f.write(payload)
    return data_write


# ---------------------------------------------------------------------- VDI

def write_vdi(path: Path, size: int, blocks_content: dict[int, bytes], kind: str = "dynamic",
              parent_uuid: uuid.UUID | None = None, zero_blocks: tuple[int, ...] = (),
              block_size: int = MIB) -> uuid.UUID:
    """BLOCKS_CONTENT: {block number: block_size bytes} stored in this file."""
    image = uuid.uuid4()
    blocks = -(-size // block_size)
    off_blocks = 512
    off_data = round_up(off_blocks + 4 * blocks, MIB)
    image_type = {"dynamic": 1, "fixed": 2, "differencing": 4}[kind]
    bmap = []
    data = bytearray()
    for b in range(blocks):
        if b in zero_blocks:
            bmap.append(0xFFFFFFFE)
        elif kind == "fixed" or b in blocks_content:
            bmap.append(len(data) // block_size)
            data += blocks_content.get(b, bytes(block_size)).ljust(block_size, b"\0")
        else:
            bmap.append(0xFFFFFFFF)
    h = bytearray(512)
    h[0:64] = b"<<< Oracle VM VirtualBox Disk Image >>>\n".ljust(64, b"\0")
    struct.pack_into("<II", h, 0x40, 0xBEDA107F, 0x00010001)
    struct.pack_into("<III", h, 0x48, 0x190, image_type, 0)
    struct.pack_into("<II", h, 0x154, off_blocks, off_data)
    struct.pack_into("<IIII", h, 0x15C, 0, 0, 0, 512)
    struct.pack_into("<QIIII", h, 0x170, size, block_size, 0, blocks, len(data) // block_size)
    h[0x188:0x198] = image.bytes_le
    h[0x198:0x1A8] = uuid.uuid4().bytes_le
    if parent_uuid is not None:
        h[0x1A8:0x1B8] = parent_uuid.bytes_le
    struct.pack_into("<IIII", h, 0x1C8, 0, 0, 0, 512)
    with open(path, "wb") as f:
        f.write(h)
        f.seek(off_blocks)
        f.write(struct.pack(f"<{blocks}I", *bmap))
        f.seek(off_data)
        f.write(data)
    return image


# ---------------------------------------------------------------------- DMG (UDIF)

DMG_ZERO, DMG_RAW, DMG_ZLIB, DMG_END = 0x00000000, 0x00000001, 0x80000005, 0xFFFFFFFF


def write_dmg(path: Path, data: bytes, chunk: int = 256 * 1024) -> None:
    """A read-only Apple disk image (UDIF) of DATA: zero chunks, raw chunks and zlib chunks
    in turn, described by a "mish" block table in the XML property list, then the "koly"
    trailer. DATA's length must be a multiple of 512."""
    assert len(data) % SECTOR == 0
    fork = bytearray()
    chunks = []
    for n, start in enumerate(range(0, len(data), chunk)):
        piece = data[start:start + chunk]
        sector, count = start // SECTOR, len(piece) // SECTOR
        if not piece.strip(b"\0"):
            chunks.append((DMG_ZERO, sector, count, len(fork), 0))
            continue
        stored = zlib.compress(piece) if n % 2 else piece
        chunks.append((DMG_ZLIB if n % 2 else DMG_RAW, sector, count, len(fork), len(stored)))
        fork += stored
    chunks.append((DMG_END, len(data) // SECTOR, 0, len(fork), 0))
    mish = bytearray(204)
    struct.pack_into(">4sIQQQII", mish, 0, b"mish", 1, 0, len(data) // SECTOR, 0, 0x208, 0)
    struct.pack_into(">I", mish, 200, len(chunks))
    for kind, sector, count, offset, length in chunks:
        mish += struct.pack(">IIQQQQ", kind, 0, sector, count, offset, length)
    plist = ('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
             '<key>resource-fork</key><dict><key>blkx</key><array><dict>'
             '<key>Attributes</key><string>0x0050</string><key>Data</key>'
             f'<data>{base64.b64encode(bytes(mish)).decode()}</data>'
             '<key>Name</key><string>whole disk</string></dict></array></dict></dict></plist>\n').encode()
    koly = bytearray(512)
    struct.pack_into(">4sIII", koly, 0, b"koly", 4, 512, 1)
    struct.pack_into(">QQQ", koly, 16, 0, 0, len(fork))         # running offset, data fork offset/length
    struct.pack_into(">II", koly, 56, 1, 1)                      # segment 1 of 1
    struct.pack_into(">QQ", koly, 216, len(fork), len(plist))    # XML offset, length
    struct.pack_into(">IQ", koly, 488, 1, len(data) // SECTOR)   # image variant, sectors
    path.write_bytes(bytes(fork) + plist + bytes(koly))
