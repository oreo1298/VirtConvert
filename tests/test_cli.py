"""The command line interface."""

import io
import json
import os
import tarfile

import pytest
from imagewriter import MIB, SECTOR, gpt_sectors, to_sectors, write_vdi, write_vhd, write_vhdx

from vertconvert import cli
from vertconvert.core import formats as F
from vertconvert.core.probe import probe


@pytest.fixture
def images(tmp_path):
    size = 8 * MIB
    gpt = gpt_sectors(size, [(2048, 8000, "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "EFI")])
    b = write_vhdx(tmp_path / "Win.vhdx", size, to_sectors(gpt))
    write_vhdx(tmp_path / "Win_ABC.avhdx", size, {9000: b"c" * SECTOR}, "differencing",
               parent=tmp_path / "Win.vhdx", parent_data_guid=b)
    write_vdi(tmp_path / "Linux.vdi", size, {0: os.urandom(MIB)})
    return tmp_path


def test_info(images, capsys):
    assert cli.main(["info", str(images / "Win_ABC.avhdx")]) == 0
    out = capsys.readouterr().out
    assert "VHDX · differencing" in out
    assert "Based on     Win.vhdx" in out
    assert "Boots with   UEFI" in out
    assert "convert to VDI" in out


def test_info_json(images, capsys):
    assert cli.main(["info", "--json", str(images / "Win.vhdx"), str(images / "Linux.vdi")]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["format"] for d in data] == ["vhdx", "vdi"]
    assert data[0]["partitions"]["firmware"] == "uefi"
    assert data[0]["virtual_size"] == 8 * MIB


def test_info_bad_file(tmp_path, capsys):
    (tmp_path / "x.vhd").write_text("nope")
    assert cli.main(["info", str(tmp_path / "x.vhd")]) == 1
    assert "✕" in capsys.readouterr().out


def test_convert_defaults_both_ways(qemu, images, capsys):
    assert cli.main(["convert", "-q", str(images / "Win_ABC.avhdx"), str(images / "Linux.vdi")]) == 0
    out = capsys.readouterr().out
    assert "verified identical" in out
    assert probe(images / "Win.vdi").format is F.VDI          # the merged checkpoint
    assert probe(images / "Linux.vhdx").format is F.VHDX      # back to Hyper-V


def test_convert_to_folder_and_format(qemu, images, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    assert cli.main(["convert", "-q", "-t", "vmdk", "-d", str(out), str(images / "Linux.vdi")]) == 0
    assert probe(out / "Linux.vmdk").format is F.VMDK


def test_convert_refuses_existing_output(qemu, images, capsys):
    (images / "Linux.vhdx").write_bytes(b"keep me")
    assert cli.main(["convert", "-q", "-y", str(images / "Linux.vdi")]) == 1
    assert "already exists" in capsys.readouterr().out
    assert (images / "Linux.vhdx").read_bytes() == b"keep me"
    assert cli.main(["convert", "-q", "-y", "--overwrite", str(images / "Linux.vdi")]) == 0
    assert probe(images / "Linux.vhdx").format is F.VHDX


def test_convert_output_option(qemu, images):
    target = images / "custom name.vhd"
    assert cli.main(["convert", "-q", "-t", "azure", "-o", str(target), str(images / "Linux.vdi")]) == 0
    info = probe(target)
    assert info.format is F.VHD and info.subformat == "fixed"


def test_output_with_several_sources_is_refused(images):
    with pytest.raises(SystemExit):
        cli.main(["convert", "-o", "x.vdi", str(images / "Win.vhdx"), str(images / "Linux.vdi")])


def test_parent_option(qemu, tmp_path, capsys):
    other = tmp_path / "parents"
    other.mkdir()
    b = write_vhd(other / "base.vhd", 4 * MIB, {0: b"p" * SECTOR})
    write_vhd(tmp_path / "child.vhd", 4 * MIB, {}, "differencing", parent=tmp_path / "base.vhd", parent_id=b)
    assert cli.main(["convert", "-q", str(tmp_path / "child.vhd")]) == 1
    assert "not found" in capsys.readouterr().out
    assert cli.main(["convert", "-q", "--parent", str(other / "base.vhd"), str(tmp_path / "child.vhd")]) == 0


def test_verify(qemu, images, capsys):
    assert cli.main(["convert", "-q", str(images / "Win.vhdx")]) == 0
    assert cli.main(["verify", "-q", str(images / "Win.vhdx"), str(images / "Win.vdi")]) == 0
    assert "identical" in capsys.readouterr().out
    assert cli.main(["verify", "-q", str(images / "Win_ABC.avhdx"), str(images / "Win.vdi")]) == 1


def test_repair_needs_nothing_for_clean_files(qemu, images, capsys):
    assert cli.main(["repair", "-y", str(images / "Win.vhdx")]) == 0
    assert "doesn't need repairing" in capsys.readouterr().out


def test_formats_and_doctor(qemu, capsys):
    assert cli.main(["formats"]) == 0
    out = capsys.readouterr().out
    assert "vhdx" in out and "azure" in out
    read_only = out.split("Read only")[1].split("Targets")[0]
    assert all(k in read_only for k in ("parallels", "dmg", "iso", "qcow ")) and "qcow2" not in read_only
    assert cli.main(["doctor"]) == 0
    assert "qemu-img" in capsys.readouterr().out


def test_missing_qemu(images, monkeypatch, capsys):
    monkeypatch.setattr(cli.QemuImg, "find", classmethod(lambda cls: None))
    assert cli.main(["convert", str(images / "Linux.vdi")]) == 1
    assert "qemu-img was not found" in capsys.readouterr().err


@pytest.mark.gui
def test_files_open_the_app(images, monkeypatch):
    opened = {}
    try:
        import vertconvert.gui.app as app
    except ImportError as exc:
        pytest.skip(f"PySide6 isn't usable: {exc}")
    monkeypatch.setattr(app, "run_gui", lambda files: opened.setdefault("files", files) and 0)
    cli.main([str(images / "Win.vhdx")])
    assert opened["files"] == [str(images / "Win.vhdx")]


def test_one_failure_does_not_stop_the_rest(qemu, images, monkeypatch, capsys):
    from vertconvert.core.errors import ConvertError
    real = cli.execute
    calls = []

    def flaky(plan, *a, **k):
        calls.append(plan.info.name)
        if len(calls) == 1:
            raise ConvertError("disk full")
        return real(plan, *a, **k)

    monkeypatch.setattr(cli, "execute", flaky)
    assert cli.main(["convert", "-q", str(images / "Win.vhdx"), str(images / "Linux.vdi")]) == 1
    out = capsys.readouterr().out
    assert "disk full" in out and calls == ["Win.vhdx", "Linux.vdi"]
    assert probe(images / "Linux.vhdx").format is F.VHDX


def test_appliance_disks_one_by_one(qemu, images, tmp_path, capsys):
    """An OVA made from a disk, then an OVA with two disks: info and convert take each disk."""
    assert cli.main(["convert", "-q", "-t", "ova", str(images / "Linux.vdi")]) == 0
    one = images / "Linux.ova"
    assert probe(one).member == "Linux-disk1.vmdk"
    with tarfile.open(one) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    ovf = members["Linux.ovf"].decode().replace('ovf:id="file1" ovf:href="Linux-disk1.vmdk"',
                                                 'ovf:id="file1" ovf:href="a/disk.vmdk"')
    ovf = ovf.replace("</References>", '<File ovf:id="file2" ovf:href="b/disk.vmdk"/></References>')
    ovf = ovf.replace("</DiskSection>", '<Disk ovf:capacity="8" ovf:capacityAllocationUnits="byte * 2^20" '
                                        'ovf:diskId="vmdisk2" ovf:fileRef="file2"/></DiskSection>')
    two = tmp_path / "Two.ova"
    with tarfile.open(two, "w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in (("Two.ovf", ovf.encode()), ("a/disk.vmdk", members["Linux-disk1.vmdk"]),
                           ("b/disk.vmdk", members["Linux-disk1.vmdk"])):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    capsys.readouterr()
    assert cli.main(["info", str(two)]) == 0
    out = capsys.readouterr().out
    assert "Two.ova › a/disk.vmdk" in out and "Two.ova › b/disk.vmdk" in out
    assert cli.main(["convert", "-q", "-t", "vdi", str(two)]) == 0
    assert sorted(p.name for p in tmp_path.glob("Two*.vdi")) == ["Two-disk-2.vdi", "Two-disk.vdi"]
    with pytest.raises(SystemExit, match="several disks"):
        cli.main(["convert", "-q", "-o", str(tmp_path / "x.vdi"), str(two)])


def test_split_vmdk(qemu, images, capsys):
    assert cli.main(["convert", "-q", "-t", "vmdk", "--split", "--fixed", str(images / "Linux.vdi")]) == 0
    assert "2 files" in capsys.readouterr().out
    assert sorted(p.name for p in images.glob("Linux*.vmdk")) == ["Linux-f001.vmdk", "Linux.vmdk"]
    assert cli.main(["convert", "-q", "-t", "vhdx", "--split", str(images / "Win.vhdx"),
                     "-o", str(images / "copy.vhdx")]) == 0
    assert "Only VMDK files can be split" in capsys.readouterr().out
