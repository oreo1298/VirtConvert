"""The other formats: OVA/OVF appliances, QED, QCOW, Parallels, DMG and ISO."""

import hashlib
import io
import os
import random
import tarfile
from pathlib import Path

import pytest
from imagewriter import MIB, SECTOR, flatten, gpt_sectors, mbr_sector, to_sectors, write_dmg, write_vhd, write_vhdx

from virtconvert.core import formats as F
from virtconvert.core import ova
from virtconvert.core.convert import Job, default_output, execute, make_plan, parse_target, verify_images
from virtconvert.core.images import expand_sources, inspect_image
from virtconvert.core.probe import probe
from virtconvert.core.qemu import image_spec, source_spec

EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
WINDOWS_DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"


def content(size, count, seed):
    rnd = random.Random(seed)
    return {s: rnd.randbytes(SECTOR) for s in rnd.sample(range(size // SECTOR), count)}


def read_all(qemu, image_spec_text: str, tmp_path: Path) -> bytes:
    out = tmp_path / "read.raw"
    qemu.check_run(["convert", image_spec_text, "-O", "raw", str(out)], "test")
    data = out.read_bytes()
    out.unlink()
    return data


def convert(qemu, source, target, out=None, member=None):
    info = inspect_image(source, qemu, member=member)
    t = parse_target(target) if isinstance(target, str) else target
    plan = make_plan(Job(source=Path(source), target=t, output=out or default_output(info, t)), qemu, info)
    assert plan.ok, plan.errors
    return plan, execute(plan, qemu)


def make_ova(path: Path, files: dict[str, bytes], descriptor: bytes | None) -> None:
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as tar:
        items = ([(path.stem + ".ovf", descriptor)] if descriptor is not None else []) + list(files.items())
        for name, data in items:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def descriptor(*disks: tuple[str, int], compression: str = "") -> bytes:
    """A minimal OVF listing DISKS (file name, capacity in MB), the way VMware writes it."""
    comp = f' ovf:compression="{compression}"' if compression else ""
    refs = "".join(f'<File ovf:href="{n}" ovf:id="file{i}"{comp}/>' for i, (n, _) in enumerate(disks))
    items = "".join(f'<Disk ovf:capacity="{c}" ovf:capacityAllocationUnits="byte * 2^20" '
                    f'ovf:diskId="vmdisk{i}" ovf:fileRef="file{i}"/>' for i, (_, c) in enumerate(disks))
    return (f'<?xml version="1.0"?><Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" '
            f'xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"><References>{refs}</References>'
            f'<DiskSection><Info>Disks</Info>{items}<Disk ovf:capacity="1" ovf:diskId="empty"/></DiskSection>'
            f'</Envelope>').encode()


# ---------------------------------------------------------------------- OVA out

def test_ova_export(qemu, tmp_path):
    size = 20 * MIB + 3 * SECTOR
    data = content(size, 600, 1)
    data.update(to_sectors(gpt_sectors(size, [(2048, 4095, EFI, "EFI"), (4096, 30000, WINDOWS_DATA, "C")])))
    write_vhdx(tmp_path / "Server.vhdx", size, data)
    plan, outcome = convert(qemu, tmp_path / "Server.vhdx", "ova")
    assert outcome.output == tmp_path / "Server.ova" and outcome.verified and outcome.files == [outcome.output]
    assert plan.size % 65536 == 0 and plan.size > size       # stream VMDK: whole grains
    assert any("Import Appliance" in n and "4 GB" in n and "Enable EFI" in n for n in plan.notes)
    assert not list(tmp_path.glob("*.part"))

    with tarfile.open(outcome.output) as tar:
        members = tar.getmembers()
        assert [m.name for m in members] == ["Server.ovf", "Server-disk1.vmdk", "Server.mf"]
        files = {m.name: tar.extractfile(m).read() for m in members}
    assert outcome.output.read_bytes()[257:263] == b"ustar\0"
    manifest = files["Server.mf"].decode().splitlines()
    assert manifest == [f"SHA256({n})= {hashlib.sha256(files[n]).hexdigest()}"
                        for n in ("Server.ovf", "Server-disk1.vmdk")]
    ovf = files["Server.ovf"].decode()
    assert "Windows10_64" in ovf and 'vmw:value="efi"' in ovf and "4096 MB of memory" in ovf
    [disk] = ova.parse_ovf(files["Server.ovf"])
    assert disk.name == "Server-disk1.vmdk" and disk.capacity == plan.size

    info = probe(outcome.output)
    assert info.ok and info.format is F.VMDK and info.member == "Server-disk1.vmdk"
    assert info.subformat == "streamOptimized" and info.virtual_size == plan.size
    assert info.name == "Server.ova › Server-disk1.vmdk" and info.stem == "Server"
    got = read_all(qemu, image_spec(info), tmp_path)
    assert got == flatten(size, data) + bytes(plan.size - size)


def test_ova_of_a_linux_disk_without_uefi(qemu, tmp_path):
    size = 8 * MIB
    data = to_sectors(mbr_sector([(2048, 8000, 0x83, True)]))
    write_vhd(tmp_path / "web server.vhd", size, data)
    _, outcome = convert(qemu, tmp_path / "web server.vhd", "ova")
    with tarfile.open(outcome.output) as tar:
        names = tar.getnames()
        ovf = tar.extractfile(names[0]).read().decode()
    assert names[0] == "web_server.ovf"            # plain names inside the archive
    assert "Linux_64" in ovf and "2048 MB of memory" in ovf and "efi" not in ovf


# ---------------------------------------------------------------------- OVA / OVF in

@pytest.fixture
def appliance(qemu, tmp_path):
    """An OVA with a stream-optimized VMDK and a VHD (with an MBR) inside."""
    size1, size2 = 4 * MIB, 6 * MIB
    one, two = content(size1, 300, 2), content(size2, 400, 3)
    two.update(mbr_sector([(2048, 4000, 0x07, True)]))
    raw = tmp_path / "one.raw"
    raw.write_bytes(flatten(size1, one))
    qemu.check_run(["convert", "-f", "raw", "-O", "vmdk", "-o", "subformat=streamOptimized", str(raw),
                    str(tmp_path / "one.vmdk")], "test")
    raw.unlink()
    write_vhd(tmp_path / "two.vhd", size2, two)
    files = {"app-disk1.vmdk": (tmp_path / "one.vmdk").read_bytes(), "app-disk2.vhd": (tmp_path / "two.vhd").read_bytes()}
    (tmp_path / "one.vmdk").unlink()
    (tmp_path / "two.vhd").unlink()
    make_ova(tmp_path / "app.ova", files, descriptor(("app-disk1.vmdk", 4), ("app-disk2.vhd", 6)))
    return tmp_path / "app.ova", {"app-disk1.vmdk": flatten(size1, one), "app-disk2.vhd": flatten(size2, two)}


def test_ova_with_several_disks(qemu, appliance, tmp_path):
    path, expected = appliance
    assert expand_sources(path) == [(path, "app-disk1.vmdk"), (path, "app-disk2.vhd")]
    whole = probe(path)
    assert whole.format is F.OVA and "contains 2 disks" in whole.errors[0]
    outputs = []
    for member, data in expected.items():
        info = inspect_image(path, qemu, member=member)
        assert info.ok, info.errors
        assert info.member_count == 2 and info.virtual_size == len(data)
        assert read_all(qemu, image_spec(info), tmp_path) == data
        plan, outcome = convert(qemu, path, "vdi", member=member)
        assert outcome.verified
        outputs.append(outcome.output.name)
        assert verify_images(info, inspect_image(outcome.output), qemu)[0]
    assert outputs == ["app-app-disk1.vdi", "app-app-disk2.vdi"]
    vhd = inspect_image(path, qemu, member="app-disk2.vhd")
    assert vhd.format is F.VHD and vhd.partitions.scheme == "mbr"     # read through qemu-img in place


def test_ova_with_one_disk_is_that_disk(qemu, tmp_path):
    write_vhd(tmp_path / "d.vhd", 2 * MIB, {0: b"hello".ljust(SECTOR, b"!")})
    make_ova(tmp_path / "solo.ova", {"d.vhd": (tmp_path / "d.vhd").read_bytes()}, descriptor(("d.vhd", 2)))
    assert expand_sources(tmp_path / "solo.ova") == [(tmp_path / "solo.ova", None)]
    info = probe(tmp_path / "solo.ova")
    assert info.ok and info.format is F.VHD and info.member == "d.vhd" and info.stem == "solo"
    _, outcome = convert(qemu, tmp_path / "solo.ova", "vhdx")
    assert outcome.output.name == "solo.vhdx"
    assert read_all(qemu, source_spec(outcome.output, "vhdx"), tmp_path)[:5] == b"hello"


def test_ova_without_descriptor_uses_its_disk_files(qemu, tmp_path):
    write_vhd(tmp_path / "d.vhd", 2 * MIB, {})
    make_ova(tmp_path / "bare.ova", {"notes.txt": b"hi", "d.vhd": (tmp_path / "d.vhd").read_bytes()}, None)
    info = probe(tmp_path / "bare.ova")
    assert info.ok and info.member == "d.vhd"


def test_compressed_ova_disk_is_refused(tmp_path):
    make_ova(tmp_path / "gz.ova", {"d.vmdk.gz": b"\x1f\x8b" + bytes(100)}, descriptor(("d.vmdk.gz", 1),
                                                                                      compression="gzip"))
    info = probe(tmp_path / "gz.ova")
    assert not info.ok and "compressed (gzip)" in info.errors[0]


def test_ova_listing_a_missing_file(tmp_path):
    make_ova(tmp_path / "broken.ova", {}, descriptor(("gone.vmdk", 1)))
    info = probe(tmp_path / "broken.ova")
    assert not info.ok and "doesn't contain it" in info.errors[0]


def test_ovf_points_at_its_disk_files(qemu, tmp_path):
    write_vhd(tmp_path / "a.vhd", 2 * MIB, {})
    write_vhd(tmp_path / "b.vhd", 2 * MIB, {})
    (tmp_path / "vm.ovf").write_bytes(descriptor(("a.vhd", 2), ("b.vhd", 2)))
    assert expand_sources(tmp_path / "vm.ovf") == [(tmp_path / "a.vhd", None), (tmp_path / "b.vhd", None)]
    info = probe(tmp_path / "vm.ovf")
    assert info.format is F.OVF and "a.vhd, b.vhd" in info.errors[0]


# ---------------------------------------------------------------------- other formats

@pytest.mark.parametrize("fmt,options", [("qed", []), ("qcow", []), ("parallels", []),
                                         ("qcow2", ["-o", "compat=0.10"])])
def test_qemu_formats_are_read(qemu, tmp_path, fmt, options):
    if qemu.missing_drivers((fmt,)):
        pytest.skip(f"this qemu-img has no {fmt} driver")
    size = 6 * MIB
    data = flatten(size, content(size, 500, 4))
    (tmp_path / "src.raw").write_bytes(data)
    image = tmp_path / f"disk.{F.BY_KEY[fmt].extension.lstrip('.')}"
    qemu.check_run(["convert", "-f", "raw", "-O", fmt, *options, str(tmp_path / "src.raw"), str(image)], "test")
    info = inspect_image(image, qemu)
    assert info.ok and info.format is F.BY_KEY[fmt] and info.virtual_size == size, info.errors
    assert not any("file name says" in n for n in info.notes)
    _, outcome = convert(qemu, image, "vdi")
    assert outcome.verified
    assert read_all(qemu, source_spec(outcome.output, "vdi"), tmp_path) == data


def test_parallels_folder(qemu, tmp_path):
    if qemu.missing_drivers(("parallels",)):
        pytest.skip("this qemu-img has no parallels driver")
    bundle = tmp_path / "Windows.hdd"
    bundle.mkdir()
    qemu.check_run(["create", "-q", "-f", "parallels", str(bundle / "Windows.hdd.0.{5fbaabe3}.hds"), "4M"], "t")
    (bundle / "DiskDescriptor.xml").write_text("<Parallels_disk_image/>")
    [(path, member)] = expand_sources(bundle)
    assert path.suffix == ".hds" and member is None
    assert probe(path).format is F.PARALLELS
    assert "is a folder" in probe(bundle).errors[0]


def test_dmg(qemu, tmp_path):
    size = 3 * MIB
    data = bytearray(flatten(size, content(size, 200, 5)))
    data[MIB:2 * MIB] = bytes(MIB)                 # a run of zeros: a zero chunk
    write_dmg(tmp_path / "Installer.dmg", bytes(data))
    info = inspect_image(tmp_path / "Installer.dmg", qemu)
    assert info.ok and info.format is F.DMG and info.virtual_size == size, info.errors
    _, outcome = convert(qemu, tmp_path / "Installer.dmg", "raw")
    assert outcome.output.name == "Installer.img" and outcome.output.read_bytes() == bytes(data)


def test_iso(qemu, tmp_path):
    data = bytearray(4 * MIB)
    data[0x8000:0x8006] = b"\x01CD001"
    data[MIB:MIB + 5] = b"files"
    (tmp_path / "live.bin").write_bytes(bytes(data))   # found by its contents, whatever the name
    info = inspect_image(tmp_path / "live.bin", qemu)
    assert info.ok and info.format is F.ISO and info.virtual_size == len(data)
    _, outcome = convert(qemu, tmp_path / "live.bin", "vdi")
    assert read_all(qemu, source_spec(outcome.output, "vdi"), tmp_path) == bytes(data)
    with pytest.raises(Exception, match="read but not written"):
        parse_target("iso")


def test_raw_by_extension_only(tmp_path):
    (tmp_path / "disk.img").write_bytes(os.urandom(MIB))
    (tmp_path / "notes.txt").write_bytes(os.urandom(MIB))
    assert probe(tmp_path / "disk.img").format is F.RAW
    assert probe(tmp_path / "notes.txt").format is None


def test_every_writable_format_has_a_test_reader():
    """Formats are listed once; the app's grid and the CLI follow the table."""
    assert F.GUI_TARGETS == tuple(f for f in F.WRITABLE)
    assert {f.key for f in F.ALL} >= {"vdi", "vmdk", "vhd", "vhdx", "qcow2", "qed", "ova", "raw", "qcow",
                                      "parallels", "dmg", "iso", "ovf"}
    assert F.default_target_for(F.VMDK) is F.VHDX and F.default_target_for(F.QCOW2) is F.VDI
