"""Writing a virtual disk onto a physical one.

Most tests write onto regular files standing in for disks (the fake_disk fixture), which runs
the whole way (qemu-img, the writable NBD server, comparing, fitting the partition table)
without root. The loop device tests at the end use a real disk: directly as root, or with
passwordless sudo (GitHub's runners), which also exercises the helper that opens the disk.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from imagewriter import MIB, SECTOR, gpt_sectors, mbr_sector, to_sectors, write_vhdx

from virtconvert import cli
from virtconvert.core import devices, ova
from virtconvert.core.errors import ConvertError, VerifyError
from virtconvert.core.nbd import NbdServer
from virtconvert.core.partitions import analyze, move_backup_gpt
from virtconvert.core.qemu import nbd_spec, spec
from virtconvert.core.writedisk import DiskWrite, disk_identity, make_write_plan, partitions_end, write_to_disk

LINUX = "0fc63daf-8483-4772-8e79-3d69d8477de4"
ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"


def gpt_disk(size: int, last_lba: int) -> bytearray:
    """A GPT disk with one partition from 1 MB to LAST_LBA, with random data at 5 MB."""
    data = bytearray(size)
    for at, chunk in gpt_sectors(size, [(2048, last_lba, LINUX, "Data")]).items():
        data[at:at + len(chunk)] = chunk
    data[5 * MIB:6 * MIB] = os.urandom(MIB)
    return data


def image(qemu, folder: Path, data: bytes, driver: str, name: str = "vm") -> Path:
    raw = folder / f"{name}.raw"
    raw.write_bytes(data)
    if driver == "raw":
        return raw
    out = folder / f"{name}.{driver}"
    options = ["-o", "force_size=on"] if driver == "vpc" else []       # the exact size, not a CHS one
    qemu.check_run(["convert", "-q", "-f", "raw", "-O", driver, *options, str(raw), str(out)], "test")
    raw.unlink()
    return out


def sfdisk_ok(path) -> bool:
    if not shutil.which("sfdisk"):
        return True
    res = subprocess.run(["sfdisk", "--verify", str(path)], capture_output=True, text=True)
    return res.returncode == 0 and "No errors detected" in res.stdout


# ---------------------------------------------------------------------- writing (onto files posing as disks)

@pytest.mark.parametrize("driver", ["vdi", "vhdx", "vpc", "qcow2", "vmdk", "raw"])
def test_write_onto_a_larger_disk(qemu, fake_disk, tmp_path, driver):
    data = gpt_disk(24 * MIB, 30000)
    source = image(qemu, tmp_path, bytes(data), driver)
    disk = fake_disk(40 * MIB)
    plan = make_write_plan(DiskWrite(source, Path(disk.path)), qemu)
    assert plan.ok and plan.size == len(data), plan.errors
    assert any("16 MB larger" in n for n in plan.notes)
    outcome = write_to_disk(plan, qemu)
    assert outcome.verified and outcome.fitted and outcome.size == len(data)
    got = Path(disk.path).read_bytes()
    assert got[2 * MIB:20 * MIB] == data[2 * MIB:20 * MIB]                  # the image
    assert got[24 * MIB:30 * MIB] == b"\xee" * (6 * MIB)                    # after it: left alone
    assert got[-MIB:-33 * SECTOR] == bytes(MIB - 33 * SECTOR)              # the old end: cleared
    assert got[len(data) - SECTOR:len(data)] == bytes(SECTOR)              # the image's old backup header
    table = analyze(got[:128 * 1024])
    assert table.scheme == "gpt" and table.partitions[0].start == MIB
    assert sfdisk_ok(disk.path)


def test_write_onto_a_smaller_disk(qemu, fake_disk, tmp_path):
    """A disk smaller than the virtual disk works when the partitions fit."""
    data = gpt_disk(32 * MIB, 20000)          # the partition ends at about 10 MB
    source = image(qemu, tmp_path, bytes(data), "vhdx")
    disk = fake_disk(16 * MIB)
    plan = make_write_plan(DiskWrite(source, Path(disk.path)), qemu)
    assert plan.ok and plan.size == 16 * MIB, plan.errors
    assert any("the first 16 MB are written" in n for n in plan.notes)
    outcome = write_to_disk(plan, qemu)
    assert outcome.verified and outcome.fitted
    got = Path(disk.path).read_bytes()
    assert got[5 * MIB:6 * MIB] == data[5 * MIB:6 * MIB] and sfdisk_ok(disk.path)
    # too small for the partitions, or a disk without any
    small = fake_disk(8 * MIB, name="sdy")
    plan = make_write_plan(DiskWrite(source, Path(small.path)), qemu)
    assert not plan.ok and "too small: 8 MB for a 32 MB virtual disk whose partitions end at" in plan.errors[0]
    plain = image(qemu, tmp_path, os.urandom(MIB) * 12, "qcow2", "plain")
    plan = make_write_plan(DiskWrite(plain, Path(small.path)), qemu)
    assert not plan.ok and "too small: 8 MB for a 12 MB virtual disk. Shrink" in plan.errors[0]


def test_write_a_checkpoint_and_an_appliance(qemu, fake_disk, tmp_path):
    size = 16 * MIB
    gpt = gpt_sectors(size, [(2048, 30000, ESP, "EFI")])
    base = write_vhdx(tmp_path / "Win.vhdx", size, to_sectors(gpt))
    write_vhdx(tmp_path / "Win_1.avhdx", size, {9000: b"c" * SECTOR}, "differencing",
               parent=tmp_path / "Win.vhdx", parent_data_guid=base)
    disk = fake_disk(size)
    plan = make_write_plan(DiskWrite(tmp_path / "Win_1.avhdx", Path(disk.path)), qemu)
    assert plan.ok and plan.uses_reader, plan.errors
    assert any("UEFI boot" in n for n in plan.notes)
    assert write_to_disk(plan, qemu).verified
    got = Path(disk.path).read_bytes()
    assert got[9000 * SECTOR:9001 * SECTOR] == b"c" * SECTOR and analyze(got[:128 * 1024]).scheme == "gpt"

    data = bytearray(size)
    data[0:SECTOR] = mbr_sector([(2048, 20000, 0x83, True)])[0]
    data[3 * MIB:4 * MIB] = os.urandom(MIB)
    raw = tmp_path / "app.raw"
    raw.write_bytes(bytes(data))
    vmdk = tmp_path / "App-disk1.vmdk"
    qemu.check_run(["convert", "-q", "-f", "raw", "-O", "vmdk", "-o", "subformat=streamOptimized", str(raw),
                    str(vmdk)], "test")
    ova.write_ova(tmp_path / "App.ova", vmdk, "App", size)
    plan = make_write_plan(DiskWrite(tmp_path / "App.ova", Path(disk.path)), qemu)
    assert plan.ok and plan.info.member, plan.errors
    assert any("legacy boot" in n for n in plan.notes)
    write_to_disk(plan, qemu)
    assert Path(disk.path).read_bytes() == bytes(data)       # the same size: nothing to fit


def test_copy_a_disk_onto_another(qemu, fake_disk, tmp_path):
    data = gpt_disk(12 * MIB, 20000)
    source = fake_disk(12 * MIB, name="sdx")
    Path(source.path).write_bytes(bytes(data))
    target = fake_disk(16 * MIB, name="sdy")
    plan = make_write_plan(DiskWrite(Path(source.path), Path(target.path)), qemu)
    assert plan.ok and plan.info.device, plan.errors
    assert write_to_disk(plan, qemu).verified
    assert Path(target.path).read_bytes()[5 * MIB:6 * MIB] == data[5 * MIB:6 * MIB]
    plan = make_write_plan(DiskWrite(Path(source.path), Path(source.path)), qemu)
    assert "the same disk" in plan.errors[-1]


def test_unmounted_first(qemu, fake_disk, tmp_path, monkeypatch):
    source = image(qemu, tmp_path, bytes(gpt_disk(4 * MIB, 6000)), "qcow2")
    disk = fake_disk(8 * MIB, mountpoints=["/run/media/me/STICK"], mounted=[("/dev/sdz1", "/run/media/me/STICK")])
    plan = make_write_plan(DiskWrite(source, Path(disk.path)), qemu)
    assert plan.ok and plan.unmount == [("/dev/sdz1", "/run/media/me/STICK")]
    assert "/run/media/me/STICK on this disk is unmounted first." in plan.warnings
    done = []
    monkeypatch.setattr(devices, "unmount", lambda dev, mp, how: done.append(dev))
    write_to_disk(plan, qemu)
    assert done == ["/dev/sdz1"]
    monkeypatch.setattr(devices, "unmount", lambda dev, mp, how: (_ for _ in ()).throw(ConvertError("busy")))
    before = Path(disk.path).read_bytes()
    with pytest.raises(ConvertError, match="busy"):
        write_to_disk(plan, qemu)
    assert Path(disk.path).read_bytes() == before         # nothing written when unmounting fails


def test_unmount_failure_is_explained(tmp_path, monkeypatch):
    tool = tmp_path / "bin" / "udisksctl"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\necho 'Error unmounting /dev/sdz1: target is busy' >&2\nexit 1\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool.parent))
    monkeypatch.setattr(devices.os, "geteuid", lambda: 1000)       # not root
    monkeypatch.setattr(devices, "elevator", lambda how: None)       # and no sudo: udisksctl only
    with pytest.raises(ConvertError, match=r"/dev/sdz1 \(mounted at /mnt\) couldn't be unmounted: Error "
                                           r"unmounting /dev/sdz1: target is busy\. Close the files"):
        devices.unmount("/dev/sdz1", "/mnt")


def test_a_wrong_copy_is_reported(qemu, fake_disk, tmp_path, monkeypatch):
    """A disk that doesn't keep what is written (failing, or a fake USB stick)."""
    disk = fake_disk(4 * MIB)
    real_write = devices.DeviceWriter.write

    def forgetful(self, offset, data):
        if offset <= 2 * MIB < offset + len(data):
            data = bytes(data[:2 * MIB - offset]) + b"\xff" * (offset + len(data) - 2 * MIB)
        real_write(self, offset, data)

    monkeypatch.setattr(devices.DeviceWriter, "write", forgetful)
    source = image(qemu, tmp_path, os.urandom(4 * MIB), "qcow2", "noise")
    plan = make_write_plan(DiskWrite(source, Path(disk.path)), qemu)
    with pytest.raises(VerifyError, match="doesn't hold what was written"):
        write_to_disk(plan, qemu)


# ---------------------------------------------------------------------- what is refused

def test_refusals(qemu, fake_disk, tmp_path, monkeypatch):
    source = image(qemu, tmp_path, bytes(gpt_disk(4 * MIB, 6000)), "qcow2")

    def errors(disk, **job):
        return make_write_plan(DiskWrite(source, Path(disk.path), **job), qemu).errors

    assert "holds the running system (in use at /, /home)" in errors(fake_disk(8 * MIB, "sda", mountpoints=["/", "/home"]))[0]
    assert "in use by luks-1 (crypt)" in errors(fake_disk(8 * MIB, "sdb", holders=["luks-1 (crypt)"]))[0]
    assert "write-protected" in errors(fake_disk(8 * MIB, "sdc", read_only=True))[0]
    assert "sdd1 is a partition" in errors(fake_disk(8 * MIB, "sdd1", kind="part"))[0]
    assert "no medium" in errors(fake_disk(0, "sde"))[0]
    assert "4096-byte sectors" in errors(fake_disk(8 * MIB, "sdf", logical_sector=4096))[0]
    disk = fake_disk(8 * MIB, "sdg")
    assert "not the disk that was chosen" in errors(disk, expect=(8 * MIB, "Other", "X"))[0]
    assert not errors(disk, expect=disk_identity(disk))
    assert "not a disk but a lvm device" in errors(fake_disk(8 * MIB, "dm-1", kind="lvm"))[0]
    assert "network block device" in errors(fake_disk(8 * MIB, "nbd0"))[0]
    assert "used as swap space" in errors(fake_disk(8 * MIB, "sdh", mountpoints=["[SWAP]"]))[0]
    assert "running system" in errors(fake_disk(8 * MIB, "sdi", mountpoints=["/run/archiso/bootmnt"]))[0]
    monkeypatch.setattr(devices, "disks_holding", lambda p: {"sdg"})
    assert "stored on Test Stick (sdg), so it would be overwritten" in errors(disk)[0]
    assert "is not a disk" in make_write_plan(DiskWrite(source, tmp_path), qemu).errors[0]


def test_convert_refuses_a_disk_as_output(qemu, fake_disk, tmp_path):
    from virtconvert.core.convert import Job, make_plan, parse_target
    source = image(qemu, tmp_path, bytes(4 * MIB), "qcow2")
    disk = fake_disk(8 * MIB)
    plan = make_plan(Job(source, parse_target("raw"), Path(disk.path), overwrite=True), qemu)
    assert "is a disk, not a file" in plan.errors[0] and "virtconvert write" in plan.errors[0]


# ---------------------------------------------------------------------- the command line

def test_write_command(qemu, fake_disk, tmp_path, monkeypatch, capsys):
    data = gpt_disk(6 * MIB, 8000)
    source = image(qemu, tmp_path, bytes(data), "vdi")
    disk = fake_disk(8 * MIB, partitions=[devices.DiskPart("sdz1", "/dev/sdz1", 7 * MIB, "vfat", "OLD")])
    with pytest.raises(SystemExit, match=r"Everything on Test Stick \(sdz\) .* now: sdz1 “OLD” \(vfat, 7 MB\)\) is "
                                         r"erased\. Add --yes"):
        cli.main(["write", str(source), disk.path])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "sdy")          # the wrong name: nothing happens
    assert cli.main(["write", str(source), disk.path]) == 1
    assert "Nothing was written" in capsys.readouterr().out
    assert Path(disk.path).read_bytes() == b"\xee" * (8 * MIB)
    monkeypatch.setattr("builtins.input", lambda _prompt: "sdz")
    assert cli.main(["write", "-q", str(source), disk.path]) == 0
    out = capsys.readouterr().out
    assert "✓ 6 MB written to Test Stick (sdz), read back and verified" in out and "fitted" in out
    assert Path(disk.path).read_bytes()[5 * MIB:6 * MIB] == data[5 * MIB:6 * MIB]
    assert cli.main(["write", str(source), disk.path, "--yes", "--no-verify"]) == 0
    assert "verified" not in capsys.readouterr().out.split("✓")[1]


# ---------------------------------------------------------------------- the pieces

def test_device_writer(tmp_path):
    path = tmp_path / "disk"
    path.write_bytes(b"\xee" * (4 * MIB))
    writer = devices.DeviceWriter(os.open(path, os.O_RDWR), 3 * MIB, "Test (sdz)")
    writer.zero(100, 5000)                    # not whole sectors, and no BLKZEROOUT on a file
    writer.write(MIB, b"abc")
    writer.flush()
    data = path.read_bytes()
    assert data[:100] == b"\xee" * 100 and data[100:5100] == bytes(5000) and data[5100:5200] == b"\xee" * 100
    assert writer.read(MIB, 3) == b"abc" and len(writer.read(3 * MIB - 10, 100)) == 10
    writer.close()
    with pytest.raises(ConvertError, match="fewer than the"):
        devices.DeviceWriter(os.open(path, os.O_RDWR), 5 * MIB, "Test (sdz)")


def test_writes_are_refused_by_a_read_only_export(qemu, tmp_path):
    source = image(qemu, tmp_path, os.urandom(MIB), "qcow2")
    path = tmp_path / "disk"
    path.write_bytes(bytes(MIB))
    reader = devices.DeviceReader(os.open(path, os.O_RDONLY), MIB, "Test (sdz)")
    with NbdServer(reader) as server:
        with pytest.raises(ConvertError):
            qemu.convert_into(spec(source, "qcow2"), nbd_spec(server.socket_path, server.export))
    reader.close()
    assert path.read_bytes() == bytes(MIB)


def test_helper_opens_for_writing(tmp_path):
    (tmp_path / "disk").write_bytes(bytes(MIB))
    fd = devices._open_with_helper(str(tmp_path / "disk"), [], lambda: False, 60, ("--any-file",), write=True)
    try:
        os.pwrite(fd, b"written", 4096)
    finally:
        os.close(fd)
    assert (tmp_path / "disk").read_bytes()[4096:4103] == b"written"
    with pytest.raises(ConvertError, match="is gone"):
        devices._open_with_helper(str(tmp_path / "missing"), [], lambda: False, 60, ("--any-file",), write=True)


def test_gpt_fitting():
    size = 32 * MIB
    disk = bytearray(size)
    for at, chunk in gpt_sectors(size, [(2048, 20000, LINUX, "Data")]).items():
        disk[at:at + len(chunk)] = chunk

    def fit(new_size: int) -> tuple[bool, bytearray]:
        copy = bytearray(new_size)
        copy[:min(size, new_size)] = disk[:min(size, new_size)]
        moved = move_backup_gpt(lambda o, n: bytes(copy[o:o + n]),
                                lambda o, d: copy.__setitem__(slice(o, o + len(d)), d), new_size, SECTOR)
        return moved, copy

    assert fit(size) == (False, disk)                                  # already right
    for new in (64 * MIB, 12 * MIB):
        moved, copy = fit(new)
        assert moved and copy[new - SECTOR:new - SECTOR + 8] == b"EFI PART"
        assert int.from_bytes(copy[446 + 12:446 + 16], "little") == new // SECTOR - 1     # protective MBR
    assert not fit(8 * MIB)[0]                                         # the partition doesn't fit
    disk[SECTOR + 30] ^= 1                                              # a damaged header is left alone
    assert not fit(64 * MIB)[0]


# ---------------------------------------------------------------------- real disks (loop devices)

def _run(cmd, root, **kw):
    return subprocess.run((["sudo", "-n"] if not root else []) + cmd, capture_output=True, **kw)


@pytest.fixture
def loops(tmp_path):
    """make(size) -> a loop device full of old contents; .how is how to open it, .read(dev) its bytes."""
    root = os.geteuid() == 0
    if not root and (not shutil.which("sudo") or _run(["true"], False).returncode != 0):
        pytest.skip("needs root or passwordless sudo for a loop device")
    if not shutil.which("losetup"):
        pytest.skip("losetup is not installed")
    made = []

    def make(size: int, read_only: bool = False, data: bytes | None = None) -> str:
        backing = tmp_path / f"loop{len(made)}.img"
        backing.write_bytes(data if data is not None else b"\xee" * size)
        res = _run(["losetup", "-f", "--show"] + (["-r"] if read_only else []) + [str(backing)], root, text=True)
        if res.returncode != 0:
            pytest.skip(f"no loop device here: {res.stderr.strip()}")
        made.append(res.stdout.strip())
        return made[-1]

    make.how = "auto" if root else "sudo"
    make.root = root
    make.read = lambda dev: _run(["cat", dev], root).stdout
    yield make
    for dev in made:
        _run(["losetup", "-d", dev], root)


def test_write_onto_a_loop_device(qemu, loops, tmp_path):
    data = gpt_disk(24 * MIB, 30000)
    source = image(qemu, tmp_path, bytes(data), "qcow2")
    dev = loops(32 * MIB)
    plan = make_write_plan(DiskWrite(source, Path(dev), elevate=loops.how), qemu)
    assert plan.ok and plan.disk.kind == "loop", plan.errors
    outcome = write_to_disk(plan, qemu)
    assert outcome.verified and outcome.fitted
    got = loops.read(dev)
    assert got[2 * MIB:20 * MIB] == data[2 * MIB:20 * MIB] and got[-MIB:-33 * SECTOR] == bytes(MIB - 33 * SECTOR)
    if shutil.which("sfdisk"):
        res = _run(["sfdisk", "--verify", dev], loops.root, text=True)
        assert "No errors detected" in res.stdout, res.stdout + res.stderr
    # the disk is opened exclusively: a second writer is refused while the first holds it
    fd = devices.open_device(dev, loops.how, write=True)
    try:
        with pytest.raises(ConvertError, match="is in use"):
            devices.open_device(dev, loops.how, write=True)
    finally:
        os.close(fd)


def test_write_command_onto_a_loop_device(qemu, loops, tmp_path, monkeypatch, capsys):
    data = gpt_disk(8 * MIB, 12000)
    source = image(qemu, tmp_path, bytes(data), "vhdx")
    dev = loops(8 * MIB)
    if loops.how != "auto":
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)    # sudo, as in a terminal
    assert cli.main(["write", "-q", str(source), dev, "--yes"]) == 0
    assert "read back and verified" in capsys.readouterr().out
    assert loops.read(dev) == bytes(data)


def test_copy_a_loop_device_onto_another(qemu, loops):
    data = bytes(gpt_disk(8 * MIB, 12000))
    source, target = loops(8 * MIB, data=data), loops(12 * MIB)
    plan = make_write_plan(DiskWrite(Path(source), Path(target), elevate=loops.how), qemu)
    assert plan.ok and plan.info.device, plan.errors
    assert write_to_disk(plan, qemu).verified
    assert loops.read(target)[5 * MIB:6 * MIB] == data[5 * MIB:6 * MIB]


def test_a_write_protected_disk(qemu, loops, tmp_path):
    source = image(qemu, tmp_path, bytes(gpt_disk(4 * MIB, 6000)), "qcow2")
    dev = loops(8 * MIB, read_only=True)
    plan = make_write_plan(DiskWrite(source, Path(dev), elevate=loops.how), qemu)
    assert "write-protected" in plan.errors[0]
    # Linux may open it for writing anyway (and refuse the writes), or refuse to open it
    try:
        writer = devices.DeviceWriter(devices.open_device(dev, loops.how, write=True), MIB, "Card (loop)")
    except ConvertError as exc:
        assert "write-protected" in str(exc)
        return
    try:
        with pytest.raises(ConvertError, match="write-protected"):
            writer.write(0, bytes(SECTOR))
    finally:
        writer.close()


def test_write_onto_a_mounted_loop_device(qemu, loops, tmp_path):
    """The file system on the disk is unmounted first (here with umount, through sudo when not root)."""
    if not shutil.which("mkfs.ext4"):
        pytest.skip("mkfs.ext4 is not installed")
    dev = loops(16 * MIB)
    folder = tmp_path / "mnt"
    folder.mkdir()
    if _run(["mkfs.ext4", "-q", dev], loops.root).returncode or _run(["mount", dev, str(folder)], loops.root).returncode:
        pytest.skip("can't mount a file system here")
    try:
        assert (dev, str(folder)) in devices.disk_info(dev).mounted
        with pytest.raises(ConvertError, match="is in use: it or a partition on it is mounted"):
            os.close(devices.open_device(dev, loops.how, write=True))
        source = image(qemu, tmp_path, bytes(gpt_disk(8 * MIB, 12000)), "qcow2")
        plan = make_write_plan(DiskWrite(source, Path(dev), elevate=loops.how), qemu)
        assert plan.ok and plan.unmount == [(dev, str(folder))], plan.errors
        assert write_to_disk(plan, qemu).verified
        assert not devices.disk_info(dev).mounted
    finally:
        _run(["umount", str(folder)], loops.root)


def test_write_a_disk_of_an_appliance(qemu, fake_disk, tmp_path, capsys):
    from imagewriter import write_vhd
    from test_formats import descriptor, make_ova

    write_vhd(tmp_path / "a.vhd", 2 * MIB, {0: b"A" * SECTOR})
    write_vhd(tmp_path / "b.vhd", 3 * MIB, {0: b"B" * SECTOR})
    make_ova(tmp_path / "app.ova", {n: (tmp_path / n).read_bytes() for n in ("a.vhd", "b.vhd")},
             descriptor(("a.vhd", 2), ("b.vhd", 3)))
    disk = fake_disk(4 * MIB)
    with pytest.raises(SystemExit, match=r"holds 2 disks \(a.vhd, b.vhd\); choose one with --member"):
        cli.main(["write", str(tmp_path / "app.ova"), disk.path, "--yes"])
    assert cli.main(["write", "-q", str(tmp_path / "app.ova"), disk.path, "--yes", "--member", "b.vhd"]) == 0
    assert Path(disk.path).read_bytes()[:SECTOR] == b"B" * SECTOR


def test_partitions_must_be_known_to_fit(qemu, fake_disk, tmp_path, monkeypatch):
    """A smaller disk only when the partitions' real positions are known and they end on it."""
    data = bytearray(64 * MIB)
    for at, chunk in gpt_sectors(64 * MIB, [(2048, 61439, LINUX, "One"), (61440, 122879, LINUX, "Two")]).items():
        data[at:at + len(chunk)] = chunk
    source = fake_disk(64 * MIB, name="sdx")
    Path(source.path).write_bytes(bytes(data))
    target = fake_disk(40 * MIB, name="sdy")
    plan = make_write_plan(DiskWrite(Path(source.path), Path(target.path)), qemu)
    assert "partitions end at 60 MB" in plan.errors[0]              # read from the disk itself
    # what lsblk says without /sys: sizes only
    source.table = "gpt"
    source.partitions = [devices.DiskPart("sdx1", "/dev/sdx1", 29 * MIB, parttype=LINUX),
                         devices.DiskPart("sdx2", "/dev/sdx2", 30 * MIB, parttype=LINUX)]
    table = source.partition_table()
    assert not table.positions_known and partitions_end(table) is None
    monkeypatch.setattr(devices.os, "access", lambda *a: False)
    info = devices.info_from_disk(source, Path(source.path))       # not readable without a password
    plan = make_write_plan(DiskWrite(Path(source.path), Path(target.path)), qemu, info)
    assert "too small: 40 MB for a 64 MB virtual disk. Shrink" in plan.errors[0]
    # a plan that believes they fit is stopped by the partition table itself, before anything is written
    monkeypatch.undo()
    plan.errors.clear()
    plan.size = target.size
    with pytest.raises(ConvertError, match="doesn't fit on Test Stick \\(sdy\\): its partitions end at 60 MB. "
                                           "Nothing was written"):
        write_to_disk(plan, qemu)
    assert Path(target.path).read_bytes() == b"\xee" * (40 * MIB)


def test_room_for_a_larger_gpt(qemu, fake_disk, tmp_path):
    """The backup of a GPT with 256 entries needs 65 sectors at the end, not 33."""
    if not shutil.which("sfdisk"):
        pytest.skip("sfdisk is not installed")
    raw = tmp_path / "big-table.raw"
    with open(raw, "wb") as f:
        f.truncate(32 * MIB)
    script = "label: gpt\ntable-length: 256\nfirst-lba: 2048\nstart=2048, size=30672, type=L\n"
    subprocess.run(["sfdisk", "-q", str(raw)], input=script, text=True, check=True, capture_output=True)
    small = fake_disk(16 * MIB)                     # 32768 sectors: the partition ends at 32719
    plan = make_write_plan(DiskWrite(raw, Path(small.path)), qemu)
    assert plan.info.partitions.entries_bytes == 256 * 128
    assert "too small" in plan.errors[0]


def test_mbr_with_4k_sectors():
    head = bytearray(128 * 1024)
    head[0:SECTOR] = mbr_sector([(256, 15360, 0x83, True)])[0]
    assert analyze(bytes(head)).partitions[0].start == 256 * 512
    table = analyze(bytes(head), 4096)
    assert table.sector_size == 4096 and table.partitions[0].start == MIB and table.partitions[0].size == 60 * MIB


def test_another_disk_under_the_same_name(qemu, fake_disk, tmp_path):
    """Checked again once the disk is open (after the password dialogs): a stick swapped meanwhile
    is not written."""
    import dataclasses
    source = image(qemu, tmp_path, bytes(gpt_disk(4 * MIB, 6000)), "qcow2")
    disk = fake_disk(8 * MIB, seq=7)
    plan = make_write_plan(DiskWrite(source, Path(disk.path)), qemu)
    fake_disk.disks[disk.path] = dataclasses.replace(disk, seq=8)
    with pytest.raises(ConvertError, match="is not the disk that was chosen any more.*Nothing was written"):
        write_to_disk(plan, qemu)
    assert Path(disk.path).read_bytes() == b"\xee" * (8 * MIB)


def test_cancelling_tells_how_far_it_got(qemu, fake_disk, tmp_path):
    from virtconvert.core.errors import OperationCancelled
    source = image(qemu, tmp_path, os.urandom(4 * MIB), "qcow2")
    disk = fake_disk(4 * MIB)
    plan = make_write_plan(DiskWrite(source, Path(disk.path)), qemu)
    with pytest.raises(OperationCancelled) as caught:
        write_to_disk(plan, qemu, cancel=lambda: True)
    assert caught.value.written is False
    stages = []
    with pytest.raises(OperationCancelled) as caught:
        write_to_disk(plan, qemu, progress=lambda stage, *_: stages.append(stage),
                      cancel=lambda: "Finishing" in stages)
    assert caught.value.written is True


def test_closing_waits_for_a_request(tmp_path):
    import threading
    path = tmp_path / "disk"
    path.write_bytes(b"\xee" * (64 * MIB))
    writer = devices.DeviceWriter(os.open(path, os.O_RDWR), 64 * MIB, "Test (sdz)")
    outcome = []

    def zero():
        try:
            writer.zero(0, 64 * MIB)
            outcome.append("done")
        except ConvertError as exc:
            outcome.append(str(exc))

    thread = threading.Thread(target=zero)
    thread.start()
    writer.close()
    thread.join()
    assert writer.fd == -1 and outcome[0] in ("done", "Writing to Test (sdz) was stopped.")


def test_open_errors():
    import errno
    assert "is in use: it or a partition on it is mounted" in devices._open_error("/dev/sdz", errno.EBUSY, "", True)
    assert "write-protected" in devices._open_error("/dev/sdz", errno.EROFS, "", True)
    assert devices._open_error("/dev/sdz", errno.EACCES, "Permission denied", True) == \
        "Cannot open /dev/sdz: Permission denied"
    assert "is gone" in devices._open_error("/dev/sdz", errno.ENOENT, "", False)


def test_erased_describes_a_whole_disk_file_system(qemu, fake_disk, tmp_path):
    source = image(qemu, tmp_path, bytes(4 * MIB), "qcow2")
    disk = fake_disk(8 * MIB, fstype="vfat", label="STICK")
    assert make_write_plan(DiskWrite(source, Path(disk.path)), qemu).erased == \
        "a vfat file system “STICK” on the whole disk"


def test_a_loop_device_of_the_image_itself(qemu, loops, tmp_path):
    source = image(qemu, tmp_path, bytes(gpt_disk(4 * MIB, 6000)), "raw")
    res = _run(["losetup", "-f", "--show", str(source)], loops.root, text=True)
    if res.returncode != 0:
        pytest.skip(res.stderr)
    dev = res.stdout.strip()
    try:
        plan = make_write_plan(DiskWrite(source, Path(dev), elevate=loops.how), qemu)
        assert f"{Path(dev).name} is a loop device of vm.raw, the image itself." in plan.errors
    finally:
        _run(["losetup", "-d", dev], loops.root)
