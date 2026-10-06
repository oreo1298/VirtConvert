"""Compare with independent implementations: VirtualBox and libvhdi.

Skipped when they aren't installed. CI installs VirtualBox for these tests.
"""

import glob
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import vboxmanage
from imagewriter import MIB, SECTOR, flatten, write_vdi, write_vhd, write_vhdx

from vertconvert.core import create as C
from vertconvert.core.chain import ChainReader
from vertconvert.core.convert import Job, execute, make_plan, parse_target
from vertconvert.core.images import inspect_image
from vertconvert.core.probe import probe
from vertconvert.core.system import find_program


def content(size, count, seed):
    rnd = random.Random(seed)
    return {s: rnd.randbytes(SECTOR) for s in rnd.sample(range(size // SECTOR), count)}


class VBox:
    def __init__(self, exe: str, home: Path):
        self.exe = exe
        self.env = dict(os.environ, VBOX_USER_HOME=str(home), LC_ALL="C")

    def run(self, *args) -> str:
        res = subprocess.run([self.exe, *args], capture_output=True, text=True, env=self.env, timeout=300)
        if res.returncode != 0:
            raise AssertionError(f"VBoxManage {' '.join(args)} failed: {res.stderr[-800:]}")
        return res.stdout

    def uuid(self, path: Path) -> str:
        out = self.run("showmediuminfo", "disk", str(path))
        return re.search(r"^UUID:\s+(\S+)", out, re.M).group(1)

    def read(self, path: Path, parents=()) -> bytes:
        for p in parents:        # VirtualBox opens a child only when its parents are registered
            self.uuid(p)
        out = path.with_name(path.name + ".vbox.raw")
        self.run("mediumio", f"--disk={path}", "cat", f"--output={out}")
        data = out.read_bytes()
        out.unlink()
        return data


@pytest.fixture
def vbox(tmp_path):
    exe = vboxmanage()
    if exe is None:
        pytest.skip("VirtualBox is not installed")
    home = tmp_path / "vbox-home"
    home.mkdir()
    return VBox(exe, home)


def convert(qemu, source: Path, target: str, out: Path, member=None):
    """TARGET: a format or preset name, optionally with -flat or -split (VMDK variants)."""
    name, _, variant = target.partition("-")
    t = parse_target(name, fixed=variant == "flat", split=variant == "split")
    info = inspect_image(source, qemu, member=member)
    plan = make_plan(Job(source=source, target=t, output=out), qemu, info)
    assert plan.ok, plan.errors
    return execute(plan, qemu)


@pytest.mark.vbox
@pytest.mark.parametrize("target,ext", [("vdi", "vdi"), ("vmdk", "vmdk"), ("vhd", "vhd"), ("vhdx", "vhdx"),
                                        ("azure", "vhd"), ("esxi", "vmdk"),
                                        ("qed", "qed"), ("vmdk-split", "vmdk"), ("vmdk-flat", "vmdk")])
def test_virtualbox_reads_our_output(qemu, vbox, tmp_path, target, ext):
    size = 10 * MIB + 3 * SECTOR
    data = content(size, 800, 1)
    data[size // SECTOR - 1] = b"END!" * 128
    write_vhdx(tmp_path / "src.vhdx", size, data)
    out = tmp_path / f"out.{ext}"
    convert(qemu, tmp_path / "src.vhdx", target, out)
    got = vbox.read(out)
    expected = flatten(size, data)
    assert got[:size] == expected
    assert got[size:] == bytes(len(got) - size)       # only zeros were added (Azure, stream VMDK)


@pytest.mark.vbox
def test_virtualbox_imports_our_ova(qemu, vbox, tmp_path):
    size = 10 * MIB + 3 * SECTOR
    data = content(size, 800, 8)
    write_vhdx(tmp_path / "src.vhdx", size, data)
    convert(qemu, tmp_path / "src.vhdx", "ova", tmp_path / "Appliance.ova")
    plan = vbox.run("import", str(tmp_path / "Appliance.ova"), "--dry-run")
    assert "Number of CPUs: 2" in plan and "Guest memory: 2048 MB" in plan, plan
    vms = tmp_path / "vms"
    vbox.run("import", str(tmp_path / "Appliance.ova"), "--vsys", "0", "--vmname", "imported",
             "--basefolder", str(vms))
    [disk] = [p for p in vms.rglob("*") if p.suffix.lower() in (".vmdk", ".vdi")]
    got = vbox.read(disk)
    assert got[:size] == flatten(size, data) and got[size:] == bytes(len(got) - size)


@pytest.mark.vbox
def test_we_read_virtualbox_ova(qemu, vbox, tmp_path):
    raw = tmp_path / "disk.raw"
    raw.write_bytes(os.urandom(8 * MIB))
    vbox.run("convertfromraw", str(raw), str(tmp_path / "disk.vdi"), "--format", "VDI")
    vbox.run("createvm", "--name", "exported", "--ostype", "Linux_64", "--register",
             "--basefolder", str(tmp_path / "vms"))
    vbox.run("storagectl", "exported", "--name", "SATA", "--add", "sata")
    vbox.run("storageattach", "exported", "--storagectl", "SATA", "--port", "0", "--type", "hdd",
             "--medium", str(tmp_path / "disk.vdi"))
    vbox.run("export", "exported", "--output", str(tmp_path / "exported.ova"))
    info = inspect_image(tmp_path / "exported.ova", qemu)
    assert info.ok and info.member and info.subformat == "streamOptimized", info.errors
    convert(qemu, tmp_path / "exported.ova", "vdi", tmp_path / "back.vdi")
    assert vbox.read(tmp_path / "back.vdi") == raw.read_bytes()


@pytest.mark.vbox
@pytest.mark.parametrize("name,fs", [("linux.vhdx", "ext4"), ("windows.vhdx", "ntfs"), ("shared.vdi", "exfat"),
                                     ("data.vmdk", "fat32"), ("empty.vhd", "")])
def test_virtualbox_reads_new_disks(qemu, vbox, tmp_path, name, fs):
    if fs and find_program(*C.FS_BY_KEY[fs].tools) is None:
        pytest.skip(f"no mkfs for {fs}")
    out = tmp_path / name
    target = parse_target(out.suffix[1:])
    disk = C.NewDisk(output=out, target=target, size=200 * MIB, scheme="gpt" if fs else "none", filesystem=fs)
    plan = C.make_create_plan(disk, qemu)
    assert plan.ok, plan.errors
    C.create_disk(plan, qemu)
    raw = tmp_path / "qemu.raw"
    qemu.check_run(["convert", "-O", "raw", str(out), str(raw)], "test")
    assert vbox.read(out) == raw.read_bytes()


@pytest.mark.vbox
def test_vhd_chain_matches_virtualbox(vbox, tmp_path):
    size = 12 * MIB + 3 * SECTOR
    base, mid, top = content(size, 3000, 2), content(size, 2000, 3), content(size, 1500, 4)
    b = write_vhd(tmp_path / "base.vhd", size, base)
    m = write_vhd(tmp_path / "mid.vhd", size, mid, "differencing", parent=tmp_path / "base.vhd", parent_id=b)
    write_vhd(tmp_path / "top.vhd", size, top, "differencing", parent=tmp_path / "mid.vhd", parent_id=m)
    theirs = vbox.read(tmp_path / "top.vhd", [tmp_path / "base.vhd", tmp_path / "mid.vhd"])
    with ChainReader.for_image(inspect_image(tmp_path / "top.vhd")) as reader:
        ours = reader.read(0, reader.size)
    assert ours == theirs == flatten(size, base, mid, top)


@pytest.mark.vbox
def test_vdi_snapshot_matches_virtualbox(vbox, tmp_path):
    size = 8 * MIB
    u = write_vdi(tmp_path / "base.vdi", size, {b: os.urandom(MIB) for b in (0, 2, 5)})
    write_vdi(tmp_path / "snap.vdi", size, {b: os.urandom(MIB) for b in (2, 3)}, "differencing",
              parent_uuid=u, zero_blocks=(5,))
    theirs = vbox.read(tmp_path / "snap.vdi", [tmp_path / "base.vdi"])
    with ChainReader.for_image(inspect_image(tmp_path / "snap.vdi")) as reader:
        assert reader.read(0, reader.size) == theirs
    assert vbox.uuid(tmp_path / "base.vdi") == probe(tmp_path / "base.vdi").disk_id


@pytest.mark.vbox
def test_virtualbox_made_checkpoint(qemu, vbox, tmp_path):
    """A differencing VHD written by VirtualBox itself, converted to VDI and read back by VirtualBox."""
    raw = tmp_path / "base.raw"
    raw.write_bytes(os.urandom(16 * MIB))
    vbox.run("convertfromraw", str(raw), str(tmp_path / "base.vhd"), "--format", "VHD")
    vbox.run("createmedium", "disk", "--filename", str(tmp_path / "child.vhd"),
             "--diffparent", str(tmp_path / "base.vhd"), "--format", "VHD")
    vbox.run("mediumio", f"--disk={tmp_path / 'child.vhd'}", "formatfat", "--quick")
    theirs = vbox.read(tmp_path / "child.vhd")
    assert theirs != raw.read_bytes()
    info = inspect_image(tmp_path / "child.vhd")
    assert [p.name for p in info.chain] == ["base.vhd"]
    assert info.disk_id == vbox.uuid(tmp_path / "child.vhd")
    convert(qemu, tmp_path / "child.vhd", "vdi", tmp_path / "merged.vdi")
    assert vbox.read(tmp_path / "merged.vdi") == theirs


def _pyvhdi_python() -> str | None:
    for exe in ["python3"] + sorted(glob.glob("/usr/bin/python3.[0-9]*"), reverse=True):
        exe = shutil.which(exe) or exe
        if not os.access(exe, os.X_OK) or exe.endswith("-config"):
            continue
        if subprocess.run([exe, "-c", "import pyvhdi"], capture_output=True).returncode == 0:
            return exe
    return None


READ_WITH_LIBVHDI = '''
import pyvhdi, sys
files = [pyvhdi.file() for _ in sys.argv[1:]]
for f, p in zip(files, sys.argv[1:]):
    f.open(p)
for child, parent in zip(files[1:], files[:-1]):
    child.set_parent(parent)
top = files[-1]
sys.stdout.buffer.write(top.read_buffer_at_offset(top.get_media_size(), 0))
'''


@pytest.mark.libvhdi
def test_vhdx_chain_matches_libvhdi(tmp_path):
    python = _pyvhdi_python()
    if python is None:
        pytest.skip("libvhdi's Python module (pyvhdi) is not installed")
    size = 20 * MIB
    base, mid, top = content(size, 3000, 5), content(size, 2000, 6), content(size, 1500, 7)
    top.update({s: os.urandom(SECTOR) for s in range(8192, 12288)})
    b = write_vhdx(tmp_path / "base.vhdx", size, base)
    m = write_vhdx(tmp_path / "mid.avhdx", size, mid, "differencing", parent=tmp_path / "base.vhdx",
                   parent_data_guid=b)
    write_vhdx(tmp_path / "top.avhdx", size, top, "differencing", parent=tmp_path / "mid.avhdx",
               parent_data_guid=m)
    files = [str(tmp_path / n) for n in ("base.vhdx", "mid.avhdx", "top.avhdx")]
    theirs = subprocess.run([python, "-c", READ_WITH_LIBVHDI, *files], capture_output=True, check=True).stdout
    with ChainReader.for_image(inspect_image(tmp_path / "top.avhdx")) as reader:
        ours = reader.read(0, reader.size)
    assert ours == theirs == flatten(size, base, mid, top)
