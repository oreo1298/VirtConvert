"""The command line interface."""

import json
import os

import pytest
from imagewriter import MIB, SECTOR, gpt_sectors, to_sectors, write_vdi, write_vhd, write_vhdx

from virtconvert import cli
from virtconvert.core import formats as F
from virtconvert.core.probe import probe


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
        import virtconvert.gui.app as app
    except ImportError as exc:
        pytest.skip(f"PySide6 isn't usable: {exc}")
    monkeypatch.setattr(app, "run_gui", lambda files: opened.setdefault("files", files) and 0)
    cli.main([str(images / "Win.vhdx")])
    assert opened["files"] == [str(images / "Win.vhdx")]
