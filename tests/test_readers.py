"""The format readers and the chain reader, against images written from the specs."""

import os
import random
import struct
import uuid

import pytest
from imagewriter import MIB, SECTOR, flatten, gpt_sectors, mbr_sector, to_sectors, write_vdi, write_vhd, write_vhdx

from virtconvert.core import formats as F
from virtconvert.core.chain import ChainReader, resolve_chain
from virtconvert.core.images import inspect_image
from virtconvert.core.partitions import analyze
from virtconvert.core.probe import probe
from virtconvert.core.vhdx import crc32c


def sectors(size, count, seed):
    rnd = random.Random(seed)
    return {s: rnd.randbytes(SECTOR) for s in rnd.sample(range(size // SECTOR), count)}


def read_all(info, chunk=None):
    with ChainReader.for_image(info) as reader:
        if chunk is None:
            return reader.read(0, reader.size)
        out = bytearray()
        rnd = random.Random(5)
        pos = 0
        while pos < reader.size:
            n = rnd.choice([SECTOR, 4096, 65536, MIB, 3 * SECTOR + 7, 1])
            out += reader.read(pos, n)
            pos += n
        return bytes(out)


def test_crc32c_known_value():
    assert crc32c(b"123456789") == 0xE3069283


@pytest.mark.parametrize("kind", ["dynamic", "fixed"])
def test_vhd_base(tmp_path, kind):
    size = 9 * MIB + 5 * SECTOR
    content = sectors(size, 900, 1)
    write_vhd(tmp_path / "d.vhd", size, content, kind)
    info = probe(tmp_path / "d.vhd")
    assert info.format is F.VHD and info.subformat == kind and info.ok
    assert info.virtual_size == size
    assert read_all(info) == flatten(size, content)
    assert read_all(info, chunk=True) == flatten(size, content)


@pytest.mark.parametrize("kind,logical", [("dynamic", 512), ("fixed", 512), ("dynamic", 4096)])
def test_vhdx_base(tmp_path, kind, logical):
    size = 11 * MIB
    rnd = random.Random(2)
    content = {s: rnd.randbytes(logical) for s in rnd.sample(range(size // logical), 300)}
    write_vhdx(tmp_path / "d.vhdx", size, content, kind, block_size=MIB, logical=logical)
    info = probe(tmp_path / "d.vhdx")
    assert info.format is F.VHDX and info.subformat == kind and info.ok
    assert info.logical_sector == logical and info.block_size == MIB
    expected = bytearray(size)
    for s, d in content.items():
        expected[s * logical:(s + 1) * logical] = d
    assert read_all(info, chunk=True) == bytes(expected)


def test_vhdx_zero_block_state(tmp_path):
    size = 6 * MIB
    content = {s: b"\x11" * SECTOR for s in range(0, 6 * 2048)}
    write_vhdx(tmp_path / "z.vhdx", size, content, block_size=MIB, zero_blocks=(2, 4))
    data = read_all(probe(tmp_path / "z.vhdx"))
    assert data[:2 * MIB] == b"\x11" * 2 * MIB
    assert data[2 * MIB:3 * MIB] == bytes(MIB)
    assert data[4 * MIB:5 * MIB] == bytes(MIB)
    assert data[5 * MIB:] == b"\x11" * MIB


def test_vdi_base(tmp_path):
    size = 5 * MIB + 512
    blocks = {0: os.urandom(MIB), 3: os.urandom(MIB), 5: os.urandom(MIB)}
    write_vdi(tmp_path / "d.vdi", size, blocks, zero_blocks=(1,))
    info = probe(tmp_path / "d.vdi")
    assert info.format is F.VDI and info.subformat == "dynamic" and info.ok
    expected = bytearray(6 * MIB)
    for b, d in blocks.items():
        expected[b * MIB:(b + 1) * MIB] = d
    assert read_all(info) == bytes(expected[:size])


def test_vhd_chain(tmp_path):
    size = 12 * MIB + 3 * SECTOR
    base, mid, top = sectors(size, 3000, 3), sectors(size, 2000, 4), sectors(size, 1500, 5)
    top.update({s: os.urandom(SECTOR) for s in range(4096, 8192)})   # one whole block
    b = write_vhd(tmp_path / "base.vhd", size, base)
    m = write_vhd(tmp_path / "mid.vhd", size, mid, "differencing", parent=tmp_path / "base.vhd", parent_id=b)
    write_vhd(tmp_path / "top.avhd", size, top, "differencing", parent=tmp_path / "mid.vhd", parent_id=m)
    info = probe(tmp_path / "top.avhd")
    assert info.differencing and info.needs_chain_reader
    resolve_chain(info)
    assert [p.name for p in info.chain] == ["mid.vhd", "base.vhd"]
    assert not info.errors
    assert read_all(info, chunk=True) == flatten(size, base, mid, top)


def test_vhdx_chain(tmp_path):
    size = 20 * MIB
    base, mid, top = sectors(size, 3000, 6), sectors(size, 2000, 7), sectors(size, 1500, 8)
    top.update({s: os.urandom(SECTOR) for s in range(8192, 12288)})  # a fully present block
    b = write_vhdx(tmp_path / "base.vhdx", size, base)
    m = write_vhdx(tmp_path / "mid.avhdx", size, mid, "differencing", parent=tmp_path / "base.vhdx",
                   parent_data_guid=b)
    write_vhdx(tmp_path / "top.avhdx", size, top, "differencing", parent=tmp_path / "mid.avhdx",
               parent_data_guid=m)
    info = inspect_image(tmp_path / "top.avhdx")
    assert [p.name for p in info.chain] == ["mid.avhdx", "base.vhdx"] and not info.errors
    assert read_all(info, chunk=True) == flatten(size, base, mid, top)


def test_vdi_snapshot_chain_via_vbox_file(tmp_path):
    vm = tmp_path / "My VM"
    (vm / "Snapshots").mkdir(parents=True)
    size = 6 * MIB
    base_blocks = {0: os.urandom(MIB), 2: os.urandom(MIB), 4: os.urandom(MIB)}
    snap_blocks = {2: os.urandom(MIB), 3: os.urandom(MIB)}
    elsewhere = tmp_path / "disks"
    elsewhere.mkdir()
    base_uuid = write_vdi(elsewhere / "system.vdi", size, base_blocks)
    snap_uuid = write_vdi(vm / "Snapshots" / "{snap}.vdi", size, snap_blocks, "differencing",
                          parent_uuid=base_uuid, zero_blocks=(4,))
    # the base is not next to the snapshot: VirtualBox's .vbox file says where it is
    (vm / "My VM.vbox").write_text(f'''<?xml version="1.0"?>
<VirtualBox xmlns="http://www.virtualbox.org/" version="1.19-linux">
  <Machine uuid="{{{uuid.uuid4()}}}" name="My VM">
    <MediaRegistry><HardDisks>
      <HardDisk uuid="{{{base_uuid}}}" location="../disks/system.vdi" format="VDI" type="Normal">
        <HardDisk uuid="{{{snap_uuid}}}" location="Snapshots/{{snap}}.vdi" format="VDI"/>
      </HardDisk>
    </HardDisks></MediaRegistry>
  </Machine>
</VirtualBox>''')
    info = inspect_image(vm / "Snapshots" / "{snap}.vdi")
    assert [p.name for p in info.chain] == ["system.vdi"], info.errors
    expected = bytearray(size)
    for b, d in base_blocks.items():
        expected[b * MIB:(b + 1) * MIB] = d
    for b, d in snap_blocks.items():
        expected[b * MIB:(b + 1) * MIB] = d
    expected[4 * MIB:5 * MIB] = bytes(MIB)
    assert read_all(info) == bytes(expected)


def test_parent_found_by_id_after_rename(tmp_path):
    size = 4 * MIB
    b = write_vhdx(tmp_path / "base.vhdx", size, {0: b"b" * SECTOR})
    write_vhdx(tmp_path / "child.avhdx", size, {1: b"c" * SECTOR}, "differencing",
               parent=tmp_path / "base.vhdx", parent_data_guid=b)
    (tmp_path / "base.vhdx").rename(tmp_path / "renamed.vhdx")
    info = inspect_image(tmp_path / "child.avhdx")
    assert [p.name for p in info.chain] == ["renamed.vhdx"]


def test_parent_name_matched_case_insensitively(tmp_path):
    size = 4 * MIB
    b = write_vhd(tmp_path / "Base.VHD", size, {0: b"b" * SECTOR})
    write_vhd(tmp_path / "child.vhd", size, {}, "differencing", parent=tmp_path / "base.vhd", parent_id=b)
    info = inspect_image(tmp_path / "child.vhd")
    assert [p.name for p in info.chain] == ["Base.VHD"]


def test_missing_parent_is_an_error(tmp_path):
    size = 4 * MIB
    write_vhdx(tmp_path / "child.avhdx", size, {}, "differencing", parent=tmp_path / "gone.vhdx",
               parent_data_guid=uuid.uuid4())
    info = inspect_image(tmp_path / "child.avhdx")
    assert not info.ok
    assert any("gone.vhdx" in e and "not found" in e for e in info.errors)


def test_modified_parent_is_refused_unless_forced(tmp_path):
    size = 4 * MIB
    write_vhdx(tmp_path / "base.vhdx", size, {0: b"b" * SECTOR})
    # the child refers to an older state of the parent
    write_vhdx(tmp_path / "child.avhdx", size, {1: b"c" * SECTOR}, "differencing",
               parent=tmp_path / "base.vhdx", parent_data_guid=uuid.uuid4())
    info = inspect_image(tmp_path / "child.avhdx")
    assert not info.ok and any("different ID" in e for e in info.errors)
    forced = inspect_image(tmp_path / "child.avhdx", parents={tmp_path / "child.avhdx": tmp_path / "base.vhdx"},
                           force=True)
    assert forced.ok and [p.name for p in forced.chain] == ["base.vhdx"]
    assert any("anyway" in w for w in forced.warnings)
    data = read_all(forced)
    assert data[:SECTOR] == b"b" * SECTOR and data[SECTOR:2 * SECTOR] == b"c" * SECTOR


def test_candidates_are_searched(tmp_path):
    size = 4 * MIB
    other = tmp_path / "elsewhere"
    other.mkdir()
    b = write_vhd(other / "parent-copy.vhd", size, {0: b"p" * SECTOR})
    write_vhd(tmp_path / "child.vhd", size, {}, "differencing", parent=tmp_path / "missing.vhd", parent_id=b)
    assert not inspect_image(tmp_path / "child.vhd").ok
    info = inspect_image(tmp_path / "child.vhd", candidates=[other / "parent-copy.vhd"])
    assert info.ok and info.chain[0].name == "parent-copy.vhd"


def test_child_larger_than_parent_reads_zeros_past_parent(tmp_path):
    b = write_vhdx(tmp_path / "base.vhdx", 4 * MIB, {s: b"x" * SECTOR for s in range(8192)})
    write_vhdx(tmp_path / "child.avhdx", 6 * MIB, {}, "differencing", parent=tmp_path / "base.vhdx",
               parent_data_guid=b)
    info = inspect_image(tmp_path / "child.avhdx")
    data = read_all(info)
    assert len(data) == 6 * MIB
    assert data[:4 * MIB] == b"x" * 4 * MIB and data[4 * MIB:] == bytes(2 * MIB)
    assert any("resized" in n for n in info.notes)


def test_padding_adds_zeros(tmp_path):
    write_vhd(tmp_path / "d.vhd", 3 * MIB, {0: b"a" * SECTOR})
    info = probe(tmp_path / "d.vhd")
    with ChainReader.for_image(info, 4 * MIB) as reader:
        assert reader.size == 4 * MIB
        data = reader.read(0, 4 * MIB)
    assert data[:SECTOR] == b"a" * SECTOR and data[3 * MIB:] == bytes(MIB)


# ---------------------------------------------------------------------- probing


def test_fixed_vhd_is_not_mistaken_for_raw(tmp_path):
    write_vhd(tmp_path / "disk.img", 2 * MIB, {}, "fixed")
    info = probe(tmp_path / "disk.img")
    assert info.format is F.VHD and info.subformat == "fixed"


def test_name_and_content_disagree(tmp_path):
    write_vhdx(tmp_path / "disk.vhd", 2 * MIB, {})
    info = probe(tmp_path / "disk.vhd")
    assert info.format is F.VHDX
    assert any("file name says VHD" in n for n in info.notes)


def test_not_an_image(tmp_path):
    (tmp_path / "notes.txt").write_text("hello")
    info = probe(tmp_path / "notes.txt")
    assert info.format is None and not info.ok


def test_truncated_fixed_vhd(tmp_path):
    write_vhd(tmp_path / "d.vhd", 2 * MIB, {}, "fixed")
    data = (tmp_path / "d.vhd").read_bytes()
    (tmp_path / "cut.vhd").write_bytes(data[:MIB] + data[-512:])
    info = probe(tmp_path / "cut.vhd")
    assert not info.ok and any("shorter" in e for e in info.errors)


def test_vhd_bad_checksum_is_a_warning(tmp_path):
    write_vhd(tmp_path / "d.vhd", 2 * MIB, {0: b"a" * SECTOR})
    data = bytearray((tmp_path / "d.vhd").read_bytes())
    data[-512 + 64] ^= 0xFF
    (tmp_path / "d.vhd").write_bytes(bytes(data))
    info = probe(tmp_path / "d.vhd")
    assert info.ok and any("checksum" in w for w in info.warnings)


def test_vhdx_damaged_header_uses_the_other(tmp_path):
    write_vhdx(tmp_path / "d.vhdx", 2 * MIB, {0: b"a" * SECTOR})
    with open(tmp_path / "d.vhdx", "r+b") as f:
        f.seek(128 * 1024 + 100)
        f.write(b"garbage")
    info = probe(tmp_path / "d.vhdx")
    assert info.ok and any("header 2" in w for w in info.warnings)
    with open(tmp_path / "d.vhdx", "r+b") as f:
        f.seek(64 * 1024 + 100)
        f.write(b"garbage")
    assert not probe(tmp_path / "d.vhdx").ok


def test_dirty_vhdx_is_reported(tmp_path):
    write_vhdx(tmp_path / "d.vhdx", 2 * MIB, {}, dirty=True)
    info = probe(tmp_path / "d.vhdx")
    assert info.dirty and not info.ok
    assert any("not closed cleanly" in e for e in info.errors)


def test_vdi_ids_are_guids(tmp_path):
    image = write_vdi(tmp_path / "d.vdi", MIB, {})
    assert probe(tmp_path / "d.vdi").disk_id == str(image)


def test_vhd_ids_are_guids(tmp_path):
    unique = write_vhd(tmp_path / "d.vhd", MIB, {})
    assert probe(tmp_path / "d.vhd").disk_id == str(unique)


def test_qcow2_and_vmdk_headers(tmp_path):
    qcow = bytearray(512)
    struct.pack_into(">4sIQIIQI", qcow, 0, b"QFI\xfb", 3, 0, 0, 16, 10 * MIB, 0)
    (tmp_path / "d.qcow2").write_bytes(bytes(qcow))
    info = probe(tmp_path / "d.qcow2")
    assert info.format is F.QCOW2 and info.virtual_size == 10 * MIB and info.block_size == 65536
    desc = (b'# Disk DescriptorFile\nversion=1\nCID=fffffffe\nparentCID=ffffffff\n'
            b'createType="monolithicFlat"\n\n# Extent description\nRW 4096 FLAT "d-flat.vmdk" 0\n')
    (tmp_path / "d.vmdk").write_bytes(desc)
    info = probe(tmp_path / "d.vmdk")
    assert info.format is F.VMDK and info.subformat == "monolithicFlat"
    assert info.virtual_size == 4096 * 512
    assert not info.ok and any("d-flat.vmdk" in e for e in info.errors)


# ---------------------------------------------------------------------- partitions


def test_gpt_disk_boots_uefi(tmp_path):
    size = 64 * MIB
    gpt = gpt_sectors(size, [(2048, 4095, "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "EFI system partition"),
                             (4096, 8191, "e3c9e316-0b5c-4db8-817d-f92df00215ae", ""),
                             (8192, 100000, "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7", "Windows")])
    write_vhdx(tmp_path / "w.vhdx", size, to_sectors(gpt))
    info = inspect_image(tmp_path / "w.vhdx")
    table = info.partitions
    assert table.scheme == "gpt" and table.firmware == "uefi"
    assert [p.kind for p in table.partitions] == ["EFI system", "Microsoft reserved", "Basic data"]
    assert table.partitions[2].start == 8192 * 512
    assert "“Windows”" in table.summary()


def test_gpt_with_4k_sectors(tmp_path):
    size = 64 * MIB
    gpt = gpt_sectors(size, [(256, 1000, "0fc63daf-8483-4772-8e79-3d69d8477de4", "root")], sector=4096)
    content = {}
    for offset, data in gpt.items():
        data = data.ljust(-(-len(data) // 4096) * 4096, b"\0")
        for i in range(0, len(data), 4096):
            content[offset // 4096 + i // 4096] = data[i:i + 4096]
    write_vhdx(tmp_path / "k.vhdx", size, content, logical=4096)
    info = inspect_image(tmp_path / "k.vhdx")
    assert info.partitions.scheme == "gpt" and info.partitions.sector_size == 4096
    assert info.partitions.partitions[0].start == 256 * 4096


def test_mbr_disk_boots_bios(tmp_path):
    write_vdi(tmp_path / "u.vdi", 4 * MIB, {0: mbr_sector([(2048, 4000, 0x83, True), (6048, 2000, 0x82, False)])[0]})
    info = inspect_image(tmp_path / "u.vdi")
    assert info.partitions.scheme == "mbr" and info.partitions.firmware == "bios"
    assert [p.kind for p in info.partitions.partitions] == ["Linux", "Linux swap"]
    assert info.partitions.partitions[0].bootable


def test_blank_disk_has_no_table():
    assert analyze(bytes(65536)).scheme == "none"
    fat = bytearray(512)
    fat[0:3] = b"\xeb\x3c\x90"
    fat[0x1C6:0x1CA] = b"\x12\x34\x56\x78"
    fat[510:512] = b"\x55\xaa"
    assert analyze(bytes(fat)).scheme == "none"
