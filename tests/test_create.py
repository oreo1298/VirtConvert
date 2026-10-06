"""Creating new disks: empty, partitioned and formatted."""

import os
import shutil
import struct
import subprocess
import uuid
import zlib
from pathlib import Path

import pytest
from imagewriter import MIB, SECTOR

from vertconvert import cli
from vertconvert.core import create as C
from vertconvert.core import formats as F
from vertconvert.core import partitions as P
from vertconvert.core import system
from vertconvert.core.convert import parse_target
from vertconvert.core.errors import ConvertError, OperationCancelled
from vertconvert.core.images import inspect_image
from vertconvert.core.probe import probe
from vertconvert.core.qemu import source_spec

GIB = 1 << 30
TIB = 1 << 40
FSCK = {
    "fat32": ["fsck.fat", "-n"], "exfat": ["fsck.exfat", "-n"], "ntfs": ["ntfsfix", "-n"],
    "ext4": ["e2fsck", "-fn"], "btrfs": ["btrfs", "check"], "xfs": ["xfs_repair", "-n"],
}
BLKID_TYPE = {"fat32": "vfat", "exfat": "exfat", "ntfs": "ntfs", "ext4": "ext4", "btrfs": "btrfs", "xfs": "xfs"}


def new(qemu, tmp_path, name, size, target=None, **kw):
    t = target or parse_target(F.by_extension(name).key)
    plan = C.make_create_plan(C.NewDisk(output=tmp_path / name, target=t, size=size, **kw), qemu)
    assert plan.ok, plan.errors
    return plan, C.create_disk(plan, qemu)


def as_raw(qemu, path: Path, driver: str) -> bytes:
    out = path.with_name(path.name + ".raw")
    qemu.check_run(["convert", source_spec(path, driver), "-O", "raw", str(out)], "test")
    data = out.read_bytes()
    out.unlink()
    return data


def tool(name):
    return system.find_program(name)


# ---------------------------------------------------------------------- pieces

def test_parse_size():
    assert C.parse_size("20G") == 20 * GIB
    assert C.parse_size("512 MB") == 512 * MIB
    assert C.parse_size("1.5T") == 3 * TIB // 2
    assert C.parse_size("2048") == 2 * GIB          # plain numbers are megabytes
    assert C.parse_size("64GiB") == 64 * GIB
    for bad in ("", "lots", "-1G", "0"):
        with pytest.raises(ConvertError):
            C.parse_size(bad)


def test_clean_label():
    fs = C.FS_BY_KEY
    assert C.clean_label(fs["fat32"], "My Data, disk!") == "MY DATA DIS"
    assert C.clean_label(fs["exfat"], "Shared  between  us") == "Shared betw"
    assert C.clean_label(fs["ext4"], "x" * 20) == "x" * 16
    assert C.clean_label(fs["ext4"], "ü" * 10) == "ü" * 8           # 16 bytes, no half character
    assert C.clean_label(fs["ntfs"], "Daten für später") == "Daten für später"


def test_single_partition_is_aligned():
    for scheme in ("gpt", "mbr"):
        for size in (8 * MIB, 20 * GIB + 3 * MIB, 2 * TIB):
            offset, length = P.single_partition(size, scheme)
            assert offset == MIB and length % MIB == 0
            end = offset + length
            if scheme == "gpt":
                assert end <= size - 33 * SECTOR and size - 33 * SECTOR - end < MIB
            else:
                assert end <= size and size - end < MIB


def test_gpt_table_follows_the_spec():
    size = 64 * MIB
    table = P.gpt_table(size, [(MIB, 60 * MIB, P.LINUX_DATA, "Home")])
    disk = bytearray(size)
    for at, data in table.items():
        disk[at:at + len(data)] = data
    lbas = size // SECTOR
    for header_lba, entries_lba, other in ((1, 2, lbas - 1), (lbas - 1, lbas - 33, 1)):
        h = disk[header_lba * SECTOR:header_lba * SECTOR + 92]
        assert h[:8] == b"EFI PART"
        crc = struct.unpack_from("<I", h, 16)[0]
        assert zlib.crc32(h[:16] + b"\0\0\0\0" + h[20:]) == crc
        current, backup, first, last = struct.unpack_from("<QQQQ", h, 24)
        assert (current, backup, first, last) == (header_lba, other, 34, lbas - 34)
        e_lba, count, esize, ecrc = struct.unpack_from("<QIII", h, 72)
        assert (e_lba, count, esize) == (entries_lba, 128, 128)
        assert zlib.crc32(disk[e_lba * SECTOR:e_lba * SECTOR + 128 * 128]) == ecrc
    entry = disk[2 * SECTOR:2 * SECTOR + 128]
    assert uuid.UUID(bytes_le=bytes(entry[:16])) == uuid.UUID(P.LINUX_DATA)
    assert struct.unpack_from("<QQ", entry, 32) == (2048, 2048 + 60 * 2048 - 1)
    assert disk[450] == 0xEE and disk[510:512] == b"\x55\xaa"     # protective MBR
    parsed = P.analyze(bytes(disk[:64 * 1024]))
    assert parsed.scheme == "gpt" and parsed.partitions[0].name == "Home"
    assert parsed.firmware == ""                                   # a data disk, not bootable
    if tool("sgdisk"):
        (Path(os.environ["XDG_CONFIG_HOME"]) / "gpt.raw").write_bytes(disk)
        res = subprocess.run([tool("sgdisk"), "-v", str(Path(os.environ["XDG_CONFIG_HOME"]) / "gpt.raw")],
                             capture_output=True, text=True)
        assert "No problems found" in res.stdout, res.stdout


def test_mbr_table():
    [(at, mbr)] = P.mbr_table([(MIB, 100 * MIB, 0x07)]).items()
    assert at == 0 and mbr[510:512] == b"\x55\xaa" and mbr[440:444] != bytes(4)
    status, ptype = mbr[446], mbr[450]
    first, count = struct.unpack_from("<II", mbr, 454)
    assert (status, ptype, first, count) == (0, 0x07, 2048, 100 * 2048)
    assert mbr[447:450] == bytes((32, 33, 0))          # CHS of sector 2048 with 255 heads, 63 sectors
    table = P.analyze(mbr)
    assert table.scheme == "mbr" and table.firmware == "" and table.partitions[0].size == 100 * MIB


def test_copy_allocated_keeps_holes(tmp_path):
    src = tmp_path / "part"
    with open(src, "wb") as f:
        f.truncate(64 * MIB)
        os.pwrite(f.fileno(), b"a" * 5000, 3 * MIB)
        os.pwrite(f.fileno(), bytes(MIB), 10 * MIB)       # written zeros are left out too
        os.pwrite(f.fileno(), b"z" * 100, 64 * MIB - 100)
    dst = tmp_path / "disk"
    with open(dst, "wb") as f:
        f.truncate(80 * MIB)
        written = C.copy_allocated(src, f.fileno(), MIB, lambda: False)
    data = dst.read_bytes()
    assert data[4 * MIB:4 * MIB + 5000] == b"a" * 5000 and data[65 * MIB - 100:65 * MIB] == b"z" * 100
    assert data.count(0) == len(data) - 5100
    assert written < 10 * MIB
    assert os.stat(dst).st_blocks * 512 < 16 * MIB


# ---------------------------------------------------------------------- empty disks

@pytest.mark.parametrize("target", ["vdi", "vmdk", "vhd", "vhdx", "qcow2", "qed", "raw", "ova"])
def test_empty_disk(qemu, tmp_path, target):
    t = parse_target(target)
    plan, outcome = new(qemu, tmp_path, f"new{t.format.extension}", 3 * GIB + 5, t)
    assert plan.size == 3 * GIB + MIB and outcome.verified
    assert any("rounded up" in n for n in plan.notes) and any("is empty" in n for n in plan.notes)
    if t.format is not F.RAW:
        info = probe(outcome.output)
        assert info.ok and info.virtual_size == plan.size, info.errors
        assert info.format is (F.VMDK if t.format is F.OVA else t.format)
    assert os.stat(outcome.output).st_blocks * 512 < 50 * MIB
    assert not list(tmp_path.glob("*.part"))


def test_huge_empty_vhdx(qemu, tmp_path):
    _, outcome = new(qemu, tmp_path, "big.vhdx", 64 * TIB)
    assert probe(outcome.output).virtual_size == 64 * TIB


def test_vmdk_variants(qemu, tmp_path):
    _, outcome = new(qemu, tmp_path, "flat.vmdk", 3 * GIB, parse_target("vmdk", fixed=True))
    assert [f.name for f in outcome.files] == ["flat.vmdk", "flat-flat.vmdk"]
    _, outcome = new(qemu, tmp_path, "split.vmdk", 5 * GIB, parse_target("vmdk", split=True))
    assert [f.name for f in outcome.files] == ["split.vmdk", "split-s001.vmdk", "split-s002.vmdk",
                                               "split-s003.vmdk"]
    assert probe(outcome.output).subformat == "twoGbMaxExtentSparse"


# ---------------------------------------------------------------------- formatted disks

@pytest.mark.parametrize("scheme", ["gpt", "mbr"])
@pytest.mark.parametrize("fs", [f.key for f in C.FILESYSTEMS])
def test_formatted_disk(qemu, tmp_path, fs, scheme):
    info = C.FS_BY_KEY[fs]
    if tool(info.tools[0]) is None:
        pytest.skip(f"{info.tools[0]} is not installed")
    size = max(64 * MIB, info.min_size + 4 * MIB)
    plan, outcome = new(qemu, tmp_path, "data.vdi", size, scheme=scheme, filesystem=fs, label="Projects")
    assert outcome.verified
    assert plan.summary == f"{size // MIB} MB VDI (dynamic), {scheme.upper()}, {info.label} “{plan.label}”"
    assert outcome.output.stat().st_size < 120 * MIB
    image = inspect_image(outcome.output, qemu)
    table = image.partitions
    assert table.scheme == scheme and len(table.partitions) == 1 and table.firmware == ""
    part = table.partitions[0]
    assert part.start == MIB and part.size == plan.partition[1]
    if scheme == "gpt":
        assert part.kind == ("Basic data" if info.windows else "Linux filesystem")
        assert part.name == plan.label
    # the file system itself, checked by its own tools
    disk = as_raw(qemu, outcome.output, "vdi")
    volume = tmp_path / "volume.raw"
    volume.write_bytes(disk[part.start:part.start + part.size])
    if tool("blkid"):
        found = subprocess.run([tool("blkid"), "-p", "-o", "export", str(volume)], capture_output=True,
                               text=True).stdout
        assert f"TYPE={BLKID_TYPE[fs]}" in found and "LABEL=" in found, found
    checker = FSCK[fs]
    if tool(checker[0]):
        res = subprocess.run([tool(checker[0]), *checker[1:], str(volume)], capture_output=True, text=True)
        assert res.returncode == 0, res.stdout + res.stderr
    if fs in ("fat32", "ntfs"):
        hidden = struct.unpack_from("<I", disk, part.start + 0x1C)[0]
        assert hidden == 2048                       # where Windows finds the partition


def test_linux_file_systems_get_small_vhdx_blocks(qemu, tmp_path):
    if tool("mkfs.ext4") is None or tool("mkntfs") is None:
        pytest.skip("mkfs.ext4 and mkntfs are needed")
    _, linux = new(qemu, tmp_path, "linux.vhdx", 2 * TIB, scheme="gpt", filesystem="ext4")
    _, windows = new(qemu, tmp_path, "windows.vhdx", 64 * GIB, scheme="gpt", filesystem="ntfs")
    assert probe(linux.output).block_size == MIB and probe(windows.output).block_size > MIB   # qemu's default
    assert linux.output.stat().st_size < 256 * MIB         # ext4 metadata packed together, small blocks
    assert linux.verified and windows.verified


def test_partition_without_file_system(qemu, tmp_path):
    plan, outcome = new(qemu, tmp_path, "blank.qcow2", 10 * GIB, scheme="mbr")
    assert plan.summary == "10 GB QCOW2 (dynamic), MBR, unformatted"
    assert inspect_image(outcome.output, qemu).partitions.label == "MBR, 1 partition"


def test_own_comparison_finds_differences(qemu, tmp_path):
    raw = tmp_path / "disk.raw"
    with open(raw, "wb") as f:
        f.truncate(100 * MIB)
        os.pwrite(f.fileno(), b"x" * 4096, 50 * MIB)
    qemu.check_run(["convert", "-f", "raw", "-O", "vhdx", str(raw), str(tmp_path / "d.vhdx")], "test")
    compare = C.compare_with_reader(raw, 100 * MIB)
    assert compare(tmp_path / "d.vhdx", lambda pct: None, lambda: False)[0]
    with open(raw, "r+b") as f:
        os.pwrite(f.fileno(), b"y", 99 * MIB)            # data the VHDX doesn't have
    same, detail = compare(tmp_path / "d.vhdx", lambda pct: None, lambda: False)
    assert not same and f"{99 * MIB:,}" in detail


# ---------------------------------------------------------------------- plan checks

def plan_for(qemu, tmp_path, name="x.vdi", size=GIB, **kw):
    target = kw.pop("target", None) or parse_target(F.by_extension(name).key)
    return C.make_create_plan(C.NewDisk(output=tmp_path / name, target=target, size=size, **kw), qemu)


def test_plan_errors(qemu, tmp_path):
    assert "partition table" in plan_for(qemu, tmp_path, filesystem="ext4").errors[0]
    assert "2 TB" in plan_for(qemu, tmp_path, size=3 * TIB, scheme="mbr").errors[0]
    assert "at least 302 MB" in plan_for(qemu, tmp_path, size=100 * MIB, scheme="gpt", filesystem="xfs").errors[0]
    assert "choose exFAT or NTFS" in plan_for(qemu, tmp_path, "x.vhdx", size=3 * TIB, scheme="gpt",
                                              filesystem="fat32").errors[0]
    assert "2040 GB" in plan_for(qemu, tmp_path, "x.vhd", size=3 * TIB).errors[0]
    assert "Unknown file system" in plan_for(qemu, tmp_path, scheme="gpt", filesystem="zfs").errors[0]
    assert "Enter a size" in plan_for(qemu, tmp_path, size=0).errors[0]
    (tmp_path / "taken.vdi").write_bytes(b"mine")
    assert "already exists" in plan_for(qemu, tmp_path, "taken.vdi").errors[0]
    assert plan_for(qemu, tmp_path, "taken.vdi", overwrite=True).ok
    notes = plan_for(qemu, tmp_path, scheme="gpt", filesystem="fat32", label="lower case").notes
    assert any("“LOWER CASE”" in n for n in notes) and any("4 GB" in n for n in notes)


def test_missing_mkfs_names_the_package(qemu, tmp_path, monkeypatch):
    monkeypatch.setattr(system, "find_program", lambda *names: None)
    monkeypatch.setattr(system, "package_manager", lambda: "dnf")
    plan = plan_for(qemu, tmp_path, scheme="gpt", filesystem="ntfs")
    assert "sudo dnf install ntfsprogs" in plan.errors[0]
    monkeypatch.setattr(system, "package_manager", lambda: "apt-get")
    assert "sudo apt install ntfs-3g" in plan_for(qemu, tmp_path, scheme="gpt", filesystem="ntfs").errors[0]


def test_cancel_while_formatting(qemu, tmp_path):
    if tool("mkfs.ext4") is None:
        pytest.skip("mkfs.ext4 is needed")
    plan = plan_for(qemu, tmp_path, "c.vdi", scheme="gpt", filesystem="ext4")
    stages = []

    def cancel():
        return "Formatting" in stages

    with pytest.raises(OperationCancelled):
        C.create_disk(plan, qemu, progress=lambda stage, *_: stages.append(stage), cancel=cancel)
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_mkfs_failure_is_reported(qemu, tmp_path, monkeypatch):
    if tool("mkfs.ext4") is None:
        pytest.skip("mkfs.ext4 is needed")
    plan = plan_for(qemu, tmp_path, "f.vdi", scheme="gpt", filesystem="ext4")
    monkeypatch.setattr(C, "mkfs_command", lambda *a: [shutil.which("sh"), "-c", "echo 'disk on fire' >&2; exit 3"])
    with pytest.raises(ConvertError, match="disk on fire"):
        C.create_disk(plan, qemu)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------- command line

def test_create_command(qemu, tmp_path, capsys):
    assert cli.main(["create", str(tmp_path / "empty"), "-t", "vhdx", "-s", "40G"]) == 0
    assert probe(tmp_path / "empty.vhdx").virtual_size == 40 * GIB
    if tool("mkfs.fat"):
        assert cli.main(["create", str(tmp_path / "usb.vdi"), "-s", "256M", "--fs", "fat32", "-L", "stick",
                         "--table", "mbr"]) == 0
        out = capsys.readouterr().out
        assert "MBR, FAT32 “STICK”" in out and "✓ usb.vdi" in out
    with pytest.raises(SystemExit, match="with --to"):
        cli.main(["create", str(tmp_path / "what.txt"), "-s", "1G"])
    assert cli.main(["create", str(tmp_path / "empty.vhdx"), "-s", "1G"]) == 1      # exists
