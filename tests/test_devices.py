"""Physical disks: listing them (lsblk), opening them (directly or through the helper), and
copying one into a virtual disk.

The copying tests use a loop device: directly when running as root, or with passwordless sudo
(GitHub's runners), which also exercises the helper that opens the disk as root. Without
either they are skipped.
"""

import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from imagewriter import MIB, SECTOR, gpt_sectors

from virtconvert import cli
from virtconvert.core import devices
from virtconvert.core import formats as F
from virtconvert.core.convert import Job, execute, make_plan, parse_target
from virtconvert.core.errors import ConvertError
from virtconvert.core.images import inspect_image
from virtconvert.core.probe import probe

ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
LINUX = "0fc63daf-8483-4772-8e79-3d69d8477de4"

LSBLK_NEW = {"blockdevices": [
    {"name": "sda", "kname": "sda", "type": "disk", "size": 500107862016, "model": "Samsung SSD 870 EVO 500GB",
     "vendor": "ATA     ", "serial": "S62ANJ0R", "tran": "sata", "rm": False, "ro": False, "log-sec": 512,
     "phy-sec": 512, "pttype": "gpt", "mountpoint": None, "children": [
         {"name": "sda1", "kname": "sda1", "type": "part", "size": 536870912, "parttype": ESP,
          "fstype": "vfat", "label": None, "mountpoint": "/boot/efi"},
         {"name": "sda2", "kname": "sda2", "type": "part", "size": 499570991104, "parttype": LINUX,
          "fstype": "crypto_LUKS", "mountpoint": None, "children": [
              {"name": "luks-1", "kname": "dm-0", "type": "crypt", "size": 499554213888, "fstype": "btrfs",
               "mountpoint": "/"}]}]},
    {"name": "sdb", "kname": "sdb", "type": "disk", "size": 15728640000, "model": "Flash Drive", "vendor": "SanDisk",
     "tran": "usb", "rm": True, "ro": False, "log-sec": 512, "phy-sec": 512, "pttype": "dos", "children": [
         {"name": "sdb1", "kname": "sdb1", "type": "part", "size": 15727591424, "parttype": "0x c",
          "partflags": "0x80", "fstype": "vfat", "label": "STICK", "mountpoint": "/run/media/me/STICK"}]},
    {"name": "sdc", "kname": "sdc", "type": "disk", "size": 0, "model": "Card Reader", "tran": "usb", "rm": True},
    {"name": "zram0", "kname": "zram0", "type": "disk", "size": 8589934592},
    {"name": "loop0", "kname": "loop0", "type": "loop", "size": 104857600},
    {"name": "mmcblk0", "kname": "mmcblk0", "type": "disk", "size": 31914983424, "tran": None, "rm": False,
     "pttype": "dos"},
    {"name": "mmcblk0boot0", "kname": "mmcblk0boot0", "type": "disk", "size": 4194304, "ro": True},
    {"name": "sr0", "kname": "sr0", "type": "rom", "size": 1073741824},
    {"name": "nvme0n1", "kname": "nvme0n1", "type": "disk", "size": 1024209543168, "model": "WD_BLACK SN770 1TB",
     "vendor": None, "tran": "nvme", "rm": False, "log-sec": 512, "pttype": None},
]}
# util-linux before 2.33 writes every value as a string
LSBLK_OLD = {"blockdevices": [
    {"name": "sdb", "kname": "sdb", "type": "disk", "size": "15728640000", "model": "Flash Drive    ",
     "vendor": "SanDisk ", "tran": "usb", "rm": "1", "ro": "0", "log-sec": "4096", "phy-sec": "4096",
     "pttype": "dos", "mountpoint": None, "children": [
         {"name": "sdb1", "kname": "sdb1", "type": "part", "size": "15727591424", "parttype": "0x7",
          "partflags": None, "fstype": "exfat", "label": "DATA", "mountpoint": None}]}]}


@pytest.fixture
def fake_lsblk(monkeypatch):
    def install(data):
        monkeypatch.setattr(devices, "_lsblk", lambda *paths: data["blockdevices"])
    return install


# ---------------------------------------------------------------------- listing

def test_list_disks(fake_lsblk):
    fake_lsblk(LSBLK_NEW)
    disks = devices.list_disks()
    assert [d.name for d in disks] == ["sda", "sdb", "mmcblk0", "nvme0n1"]
    sda, sdb, mmc, nvme = disks
    assert sda.title == "Samsung SSD 870 EVO 500GB (sda)"           # "ATA" is not a maker
    assert sda.mountpoints == ["/boot/efi", "/"] and sda.system      # / is inside the LUKS volume
    assert sda.summary == "466 GB · SATA · GPT, 2 partitions · in use (/boot/efi, /)"
    assert sda.partition_table().firmware == "uefi"
    assert sdb.title == "SanDisk Flash Drive (sdb)" and sdb.bus == "USB" and not sdb.system
    assert sdb.partition_table().firmware == "bios"               # an active MBR partition
    assert sdb.partitions[0].label == "STICK" and sdb.stem == "Flash Drive"
    assert nvme.summary == "954 GB · NVMe · no partition table" and nvme.title == "WD_BLACK SN770 1TB (nvme0n1)"
    assert mmc.title == "mmcblk0" and mmc.stem == "mmcblk0"
    assert [d.name for d in devices.list_disks(include_loop=True)] == ["sda", "sdb", "loop0", "mmcblk0", "nvme0n1"]


def test_old_lsblk(fake_lsblk):
    fake_lsblk(LSBLK_OLD)
    [disk] = devices.list_disks()
    assert disk.removable and not disk.read_only and disk.logical_sector == 4096 and disk.size == 15728640000
    assert disk.model == "Flash Drive" and disk.vendor == "SanDisk"
    table = disk.partition_table()
    assert table.scheme == "mbr" and table.partitions[0].kind == "NTFS/exFAT" and table.firmware == ""


def test_inspect_device_without_reading(fake_lsblk, monkeypatch):
    fake_lsblk({"blockdevices": [LSBLK_NEW["blockdevices"][1]]})
    monkeypatch.setattr(devices.os, "access", lambda *a: False)      # can't read it: lsblk only
    info = devices.inspect_device("/dev/sdb")
    assert info.ok and info.device and info.format is F.RAW and info.virtual_size == 15728640000
    assert info.name == "SanDisk Flash Drive (sdb)" and info.stem == "Flash Drive"
    assert info.subformat == "physical disk, USB" and info.mountpoints == ["/run/media/me/STICK"]
    assert info.partitions.label == "MBR, 1 partition"
    fake_lsblk({"blockdevices": [LSBLK_NEW["blockdevices"][2]]})
    assert "no medium" in devices.inspect_device("/dev/sdc").errors[0]


# ---------------------------------------------------------------------- planning

def test_plan_for_a_disk(fake_lsblk, monkeypatch, qemu, tmp_path):
    fake_lsblk({"blockdevices": [LSBLK_NEW["blockdevices"][0]]})
    monkeypatch.setattr(devices.os, "access", lambda path, mode: str(path) != "/dev/sda")
    info = devices.inspect_device("/dev/sda")
    monkeypatch.setattr(devices, "base_disks", lambda dev: {"sda"})
    monkeypatch.setattr(devices, "disks_holding", lambda path: {"sdz"})
    plan = make_plan(Job(source=Path("/dev/sda"), target=parse_target("vdi"), output=tmp_path / "pc.vdi"),
                     qemu, info)
    assert plan.ok and plan.uses_reader and not plan.can_verify
    assert any("running system" in w for w in plan.warnings)
    assert any("administrator rights" in n for n in plan.notes)
    assert any("Enable EFI" in n for n in plan.notes)                 # from the EFI system partition
    monkeypatch.setattr(devices, "disks_holding", lambda path: {"sda"})
    plan = make_plan(Job(source=Path("/dev/sda"), target=parse_target("vdi"), output=tmp_path / "pc.vdi"),
                     qemu, info)
    assert not plan.ok and "on the disk being copied (sda)" in plan.errors[0]


def test_default_output_is_not_in_dev(fake_lsblk, monkeypatch, tmp_path):
    from virtconvert.core.convert import default_output
    fake_lsblk({"blockdevices": [LSBLK_NEW["blockdevices"][8]]})
    info = devices.inspect_device("/dev/nvme0n1")
    monkeypatch.chdir(tmp_path)
    assert default_output(info, parse_target("vhdx")) == tmp_path / "WD_BLACK SN770 1TB.vhdx"


# ---------------------------------------------------------------------- opening and reading

def test_helper_hands_over_the_file(tmp_path):
    data = os.urandom(3 * MIB)
    (tmp_path / "disk").write_bytes(data)
    fd = devices._open_with_helper(str(tmp_path / "disk"), [], lambda: False, 60, ("--any-file",))
    try:
        assert os.pread(fd, 4096, MIB) == data[MIB:MIB + 4096]
    finally:
        os.close(fd)
    with pytest.raises(ConvertError, match="is not a disk"):
        devices._open_with_helper(str(tmp_path / "disk"), [], lambda: False, 60, ())
    with pytest.raises(ConvertError, match="as administrator"):
        devices._open_with_helper(str(tmp_path / "disk"), [shutil.which("false")], lambda: False, 60, ())
    assert not [p for p in Path(devices._socket_parent()).glob("virtconvert-*") if (p / "fd").exists()]


def test_elevator(monkeypatch):
    monkeypatch.setattr(devices.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert devices.elevator("auto") == ["/usr/bin/sudo", "--"]
    assert devices.elevator("pkexec") == ["/usr/bin/pkexec"]
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert devices.elevator("auto") == ["/usr/bin/pkexec"]
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert devices.elevator("auto") is None


def test_reader_pads_and_reports_bad_sectors(tmp_path, monkeypatch):
    (tmp_path / "disk").write_bytes(b"x" * (MIB + SECTOR))
    fd = os.open(tmp_path / "disk", os.O_RDONLY)
    reader = devices.DeviceReader(fd, 2 * MIB, "Test disk (sdz)")
    assert reader.size == 2 * MIB and reader.read(MIB, 1024) == b"x" * SECTOR + bytes(SECTOR)
    real = os.pread

    def failing(fd_, n, offset):
        if offset >= 4096:
            raise OSError(errno.EIO, "Input/output error")
        return real(fd_, n, offset)

    monkeypatch.setattr(devices.os, "pread", failing)
    assert reader.read(0, 4096) == b"x" * 4096
    with pytest.raises(ConvertError, match="damaged sector.*ddrescue"):
        reader.read(4096, 8192)
    reader.close()


def test_disks_holding_a_folder(tmp_path):
    held = devices.disks_holding(tmp_path)
    assert all(isinstance(n, str) and "/" not in n for n in held)


# ---------------------------------------------------------------------- copying a disk

def _run(cmd, root):
    return subprocess.run((["sudo", "-n"] if not root else []) + cmd, capture_output=True, text=True)


@pytest.fixture
def loop_disk(tmp_path):
    """A loop device with a GPT disk on it: (device, the bytes it holds, how to elevate)."""
    root = os.geteuid() == 0
    if not root and (not shutil.which("sudo") or _run(["true"], False).returncode != 0):
        pytest.skip("needs root or passwordless sudo for a loop device")
    if not shutil.which("losetup"):
        pytest.skip("losetup is not installed")
    size = 24 * MIB
    data = bytearray(size)
    for at, chunk in gpt_sectors(size, [(2048, 40000, LINUX, "Data")]).items():
        data[at:at + len(chunk)] = chunk
    data[5 * MIB:6 * MIB] = os.urandom(MIB)
    image = tmp_path / "physical.img"
    image.write_bytes(bytes(data))
    res = _run(["losetup", "-f", "--show", str(image)], root)
    if res.returncode != 0:
        pytest.skip(f"no loop device here: {res.stderr.strip()}")
    device = res.stdout.strip()
    yield device, bytes(data), "auto" if root else "sudo"
    _run(["losetup", "-d", device], root)


def test_copy_a_disk(qemu, loop_disk, tmp_path):
    device, data, how = loop_disk
    info = inspect_image(device, qemu)
    assert info.ok and info.device and info.virtual_size == len(data), info.errors
    plan = make_plan(Job(source=Path(device), target=parse_target("vhdx"), output=tmp_path / "copy.vhdx",
                         elevate=how), qemu, info)
    assert plan.ok and plan.uses_reader and plan.can_verify, plan.errors
    outcome = execute(plan, qemu)
    assert outcome.verified
    out = tmp_path / "back.raw"
    qemu.check_run(["convert", "-f", "vhdx", "-O", "raw", str(outcome.output), str(out)], "test")
    assert out.read_bytes() == data
    # padded for a stream-optimized VMDK: the extra part is zeros
    plan = make_plan(Job(source=Path(device), target=parse_target("esxi"), output=tmp_path / "copy.vmdk",
                         elevate=how), qemu, info)
    assert plan.size % 65536 == 0
    assert execute(plan, qemu).verified


def test_copy_from_the_command_line(qemu, loop_disk, tmp_path, monkeypatch, capsys):
    device, data, how = loop_disk
    if how != "auto":
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)    # sudo, as in a terminal
    assert cli.main(["create", "-q", str(tmp_path / "pc.qcow2"), "--from", device]) == 0
    assert probe(tmp_path / "pc.qcow2").virtual_size == len(data)
    assert "verified identical to the disk" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="leave out --size"):
        cli.main(["create", str(tmp_path / "x.vdi"), "--from", device, "-s", "1G"])
    with pytest.raises(SystemExit, match="is not a disk"):
        cli.main(["create", str(tmp_path / "x.vdi"), "--from", str(tmp_path)])
    assert cli.main(["disks", "--all"]) == 0
    assert Path(device).name in capsys.readouterr().out


def test_disks_command(fake_lsblk, capsys):
    fake_lsblk(LSBLK_NEW)
    assert cli.main(["disks"]) == 0
    out = capsys.readouterr().out
    assert "/dev/sda      Samsung SSD 870 EVO 500GB (sda)" in out and "sdb1: 14.6 GB  vfat “STICK”" in out
    assert "zram0" not in out and "--from /dev/sdX" in out
    assert cli.main(["disks", "--json"]) == 0
    assert '"title": "SanDisk Flash Drive (sdb)"' in capsys.readouterr().out
