"""Conversions with qemu-img: every direction, chains, padding, safety checks."""

import os
import random
import subprocess
import uuid
from pathlib import Path

import pytest
from imagewriter import MIB, SECTOR, flatten, write_vdi, write_vhd, write_vhdx

from virtconvert.core import formats as F
from virtconvert.core import vhdx as V
from virtconvert.core.convert import (
    Job,
    Target,
    default_output,
    default_target,
    execute,
    make_plan,
    parse_target,
    unique_path,
    verify_images,
)
from virtconvert.core.errors import ConvertError, OperationCancelled, VerifyError
from virtconvert.core.images import inspect_image
from virtconvert.core.nbd import NbdServer
from virtconvert.core.probe import probe
from virtconvert.core.qemu import nbd_spec, source_spec


def as_raw(qemu, path: Path, driver: str) -> bytes:
    out = path.with_suffix(path.suffix + ".raw")
    qemu.check_run(["convert", source_spec(path, driver), "-O", "raw", str(out)], "test")
    data = out.read_bytes()
    out.unlink()
    return data


def content(size, count, seed):
    rnd = random.Random(seed)
    return {s: rnd.randbytes(SECTOR) for s in rnd.sample(range(size // SECTOR), count)}


@pytest.fixture
def vhdx_source(tmp_path):
    size = 24 * MIB
    data = content(size, 2000, 1)
    path = tmp_path / "Server.vhdx"
    write_vhdx(path, size, data)
    return path, flatten(size, data)


def run(qemu, source, target, out=None, **kw):
    info = inspect_image(source, qemu)
    out = out or default_output(info, target)
    plan = make_plan(Job(source=Path(source), target=target, output=out, **kw), qemu, info)
    assert plan.ok, plan.errors
    return plan, execute(plan, qemu)


@pytest.mark.parametrize("target", ["vdi", "vmdk", "vhd", "vhdx", "qcow2", "raw"])
@pytest.mark.parametrize("fixed", [False, True])
def test_every_format(qemu, vhdx_source, target, fixed):
    source, expected = vhdx_source
    t = parse_target(target, fixed=fixed)
    plan, outcome = run(qemu, source, t)
    assert outcome.verified and outcome.output.exists()
    assert not Path(str(outcome.output) + ".part").exists()
    info = probe(outcome.output)
    assert info.format is t.format, info.errors
    if fixed and t.format in (F.VDI, F.VHD, F.VHDX):
        assert info.subformat == "fixed"
    assert as_raw(qemu, outcome.output, t.format.driver) == expected


def test_back_to_hyperv(qemu, tmp_path):
    size = 10 * MIB
    blocks = {0: os.urandom(MIB), 7: os.urandom(MIB)}
    write_vdi(tmp_path / "vm.vdi", size, blocks)
    info = probe(tmp_path / "vm.vdi")
    target = default_target(info)
    assert target.format is F.VHDX
    _, outcome = run(qemu, tmp_path / "vm.vdi", target)
    assert outcome.output.name == "vm.vhdx"
    expected = bytearray(size)
    for b, d in blocks.items():
        expected[b * MIB:(b + 1) * MIB] = d
    assert as_raw(qemu, outcome.output, "vhdx") == bytes(expected)


def test_vhdx_output_marks_regions_required(qemu, vhdx_source):
    source, _ = vhdx_source
    _, outcome = run(qemu, source, parse_target("vhdx"), out=source.with_name("out.vhdx"))
    with open(outcome.output, "rb") as f:
        f.seek(V.REGION_OFFSETS[0])
        table = f.read(64 * 1024)
    assert V.checksum_ok(table)
    flags = [int.from_bytes(table[16 + 32 * n + 28:16 + 32 * n + 32], "little") for n in range(2)]
    assert flags == [1, 1]


@pytest.mark.parametrize("fmt", ["vhd", "vhdx", "vdi"])
def test_chain_is_merged(qemu, tmp_path, fmt):
    size = 16 * MIB
    if fmt == "vdi":
        base_blocks = {b: os.urandom(MIB) for b in (0, 3, 9)}
        snap_blocks = {b: os.urandom(MIB) for b in (3, 4)}
        u = write_vdi(tmp_path / "base.vdi", size, base_blocks)
        write_vdi(tmp_path / "snap.vdi", size, snap_blocks, "differencing", parent_uuid=u)
        top = tmp_path / "snap.vdi"
        expected = bytearray(size)
        for blocks in (base_blocks, snap_blocks):
            for b, d in blocks.items():
                expected[b * MIB:(b + 1) * MIB] = d
        expected = bytes(expected)
    else:
        base, child = content(size, 1500, 2), content(size, 900, 3)
        if fmt == "vhd":
            b = write_vhd(tmp_path / "base.vhd", size, base)
            write_vhd(tmp_path / "child.avhd", size, child, "differencing", parent=tmp_path / "base.vhd",
                      parent_id=b)
            top = tmp_path / "child.avhd"
        else:
            b = write_vhdx(tmp_path / "base.vhdx", size, base)
            write_vhdx(tmp_path / "child.avhdx", size, child, "differencing",
                       parent=tmp_path / "base.vhdx", parent_data_guid=b)
            top = tmp_path / "child.avhdx"
        expected = flatten(size, base, child)
    target = parse_target("vhdx" if fmt == "vdi" else "vdi")
    plan, outcome = run(qemu, top, target)
    assert plan.uses_reader
    assert outcome.verified
    # named after the disk the checkpoint belongs to
    assert outcome.output.stem == "base"
    assert as_raw(qemu, outcome.output, target.format.driver) == expected


def test_qemu_alone_would_get_a_differencing_vhd_wrong(qemu, tmp_path):
    """Why VirtConvert merges chains itself: qemu-img ignores a VHD's parent without an error."""
    size = 4 * MIB
    b = write_vhd(tmp_path / "base.vhd", size, {0: b"p" * SECTOR})
    write_vhd(tmp_path / "child.vhd", size, {1: b"c" * SECTOR}, "differencing", parent=tmp_path / "base.vhd",
              parent_id=b)
    naive = as_raw(qemu, tmp_path / "child.vhd", "vpc")
    assert naive[:SECTOR] != b"p" * SECTOR
    _, outcome = run(qemu, tmp_path / "child.vhd", parse_target("raw"))
    good = outcome.output.read_bytes()
    assert good[:SECTOR] == b"p" * SECTOR and good[SECTOR:2 * SECTOR] == b"c" * SECTOR


def test_full_size_of_virtualbox_made_vhd(qemu, tmp_path):
    """qemu-img would size a VHD from an unknown program by its CHS geometry and cut off the end."""
    size = 33554432
    data = {size // SECTOR - 1: b"E" * SECTOR}
    write_vhd(tmp_path / "vbox.vhd", size, data, creator=b"vbox")
    _, outcome = run(qemu, tmp_path / "vbox.vhd", parse_target("vdi"))
    assert probe(outcome.output).virtual_size == size
    assert as_raw(qemu, outcome.output, "vdi")[-SECTOR:] == b"E" * SECTOR


def test_azure_rounds_up_to_whole_megabytes(qemu, tmp_path):
    size = 5 * MIB + 3 * SECTOR
    data = {size // SECTOR - 1: b"z" * SECTOR}
    write_vdi(tmp_path / "small.vdi", size, {5: (bytes(3 * SECTOR - SECTOR) + b"z" * SECTOR).ljust(MIB, b"\0")})
    plan, outcome = run(qemu, tmp_path / "small.vdi", parse_target("azure"))
    assert plan.size == 6 * MIB
    info = probe(outcome.output)
    assert info.format is F.VHD and info.subformat == "fixed" and info.virtual_size == 6 * MIB
    assert outcome.output.stat().st_size == 6 * MIB + 512
    raw = as_raw(qemu, outcome.output, "vpc")
    assert raw[size - SECTOR:size] == data[size // SECTOR - 1] and raw[size:] == bytes(6 * MIB - size)


def test_stream_vmdk_is_padded_to_grains(qemu, tmp_path):
    size = 3 * MIB + 3 * SECTOR
    write_vhd(tmp_path / "odd.vhd", size, {size // SECTOR - 1: b"t" * SECTOR})
    plan, outcome = run(qemu, tmp_path / "odd.vhd", parse_target("ova"))
    assert plan.size % 65536 == 0 and plan.size >= size
    assert probe(outcome.output).subformat == "streamOptimized"
    raw = as_raw(qemu, outcome.output, "vmdk")
    assert raw[size - SECTOR:size] == b"t" * SECTOR


def test_fixed_vhd_grows_without_the_reader(qemu, tmp_path):
    """Sources VirtConvert can't read itself (QCOW2) grow by creating the target first."""
    q = tmp_path / "d.qcow2"
    qemu.check_run(["create", "-q", "-f", "qcow2", str(q), str(MIB + 512)], "test")
    plan, outcome = run(qemu, q, parse_target("azure"))
    assert not plan.uses_reader and plan.size == 2 * MIB
    assert probe(outcome.output).virtual_size == 2 * MIB


# ---------------------------------------------------------------------- plans and safety


def test_existing_output_is_not_replaced(qemu, vhdx_source):
    source, _ = vhdx_source
    out = source.with_name("taken.vdi")
    out.write_bytes(b"precious")
    plan = make_plan(Job(source=source, target=Target(F.VDI), output=out), qemu)
    assert not plan.ok and any("already exists" in e for e in plan.errors)
    plan = make_plan(Job(source=source, target=Target(F.VDI), output=out, overwrite=True), qemu)
    assert plan.ok
    execute(plan, qemu)
    assert probe(out).format is F.VDI


def test_output_must_not_be_the_source(qemu, vhdx_source):
    source, _ = vhdx_source
    plan = make_plan(Job(source=source, target=Target(F.VHDX), output=source, overwrite=True), qemu)
    assert not plan.ok and any("overwrite the source" in e for e in plan.errors)


def test_too_big_for_vhd(qemu, tmp_path):
    write_vhdx(tmp_path / "huge.vhdx", 3 << 40, {}, block_size=256 * MIB)
    plan = make_plan(Job(source=tmp_path / "huge.vhdx", target=Target(F.VHD),
                         output=tmp_path / "huge.vhd"), qemu)
    assert not plan.ok and any("2040 GB" in e and "VHDX" in e for e in plan.errors)


def test_uefi_and_sector_notes(qemu, tmp_path):
    from imagewriter import gpt_sectors, to_sectors
    size = 8 * MIB
    gpt = gpt_sectors(size, [(2048, 8000, "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "EFI")])
    write_vhdx(tmp_path / "efi.vhdx", size, to_sectors(gpt))
    plan = make_plan(Job(source=tmp_path / "efi.vhdx", target=Target(F.VDI), output=tmp_path / "efi.vdi"), qemu)
    assert any("Enable EFI" in n for n in plan.notes)
    plan = make_plan(Job(source=tmp_path / "efi.vhdx", target=Target(F.VHD), output=tmp_path / "efi.vhd"), qemu)
    assert any("generation 2" in w for w in plan.warnings)
    write_vhdx(tmp_path / "4k.vhdx", size, {}, logical=4096)
    plan = make_plan(Job(source=tmp_path / "4k.vhdx", target=Target(F.VDI), output=tmp_path / "4k.vdi"), qemu)
    assert any("4 KB sectors" in w for w in plan.warnings)


def test_cancel_removes_the_partial_file(qemu, vhdx_source):
    source, _ = vhdx_source
    out = source.with_name("cancelled.vdi")
    plan = make_plan(Job(source=source, target=Target(F.VDI), output=out), qemu)
    with pytest.raises(OperationCancelled):
        execute(plan, qemu, cancel=lambda: True)
    assert not out.exists() and not Path(str(out) + ".part").exists()


def test_source_changing_is_detected(qemu, vhdx_source):
    source, _ = vhdx_source
    out = source.with_name("changed.vdi")
    plan = make_plan(Job(source=source, target=Target(F.VDI), output=out, verify=False), qemu)

    def touch(stage, done, total):
        if done == total:
            os.utime(source, ns=(1, 1))

    with pytest.raises(ConvertError, match="changed while"):
        execute(plan, qemu, progress=touch)
    assert not out.exists()


def test_verify_failure_deletes_the_copy(qemu, vhdx_source, monkeypatch):
    source, _ = vhdx_source
    out = source.with_name("bad.vdi")
    plan = make_plan(Job(source=source, target=Target(F.VDI), output=out), qemu)
    monkeypatch.setattr(type(qemu), "compare", lambda *a, **k: (False, "Content mismatch at offset 0!"))
    with pytest.raises(VerifyError):
        execute(plan, qemu)
    assert not out.exists() and not Path(str(out) + ".part").exists()


def test_progress_is_reported(qemu, vhdx_source):
    source, _ = vhdx_source
    seen = []
    plan = make_plan(Job(source=source, target=Target(F.VDI), output=source.with_name("p.vdi")), qemu)
    execute(plan, qemu, progress=lambda s, d, t: seen.append((s, d, t)))
    stages = [s for s, _, _ in seen]
    assert "Converting" in stages and "Verifying" in stages
    assert all(0 <= d <= t for _, d, t in seen)
    assert seen[-1][1] == seen[-1][2]


def test_verify_images(qemu, vhdx_source, tmp_path):
    source, _ = vhdx_source
    _, outcome = run(qemu, source, Target(F.VMDK))
    same, _ = verify_images(probe(source), probe(outcome.output), qemu)
    assert same
    other = tmp_path / "other.vhdx"
    write_vhdx(other, 24 * MIB, {0: b"x" * SECTOR})
    same, detail = verify_images(probe(source), probe(other), qemu)
    assert not same and "mismatch" in detail.lower()


def test_names(tmp_path):
    (tmp_path / "Snapshots").mkdir()
    b = write_vdi(tmp_path / "VM.vdi", MIB, {})
    write_vdi(tmp_path / "Snapshots" / "{x}.vdi", MIB, {}, "differencing", parent_uuid=b)
    info = inspect_image(tmp_path / "Snapshots" / "{x}.vdi")
    assert default_output(info, Target(F.VHDX)) == tmp_path / "VM.vhdx"
    (tmp_path / "VM.vhdx").write_bytes(b"")
    assert default_output(info, Target(F.VHDX)) == tmp_path / "VM-2.vhdx"
    assert unique_path(tmp_path / "a.vdi", {tmp_path / "a.vdi"}) == tmp_path / "a-2.vdi"
    plain = probe(tmp_path / "VM.vdi")
    assert default_output(plain, Target(F.VDI)) == tmp_path / "VM-converted.vdi"


def test_parse_target():
    assert parse_target("hyperv").format is F.VHDX
    t = parse_target("azure")
    assert t.format is F.VHD and t.fixed and t.azure
    t = parse_target("ova")
    assert t.format is F.VMDK and t.stream and not t.fixed
    assert parse_target("vmdk", fixed=True).fixed is False   # no single-file fixed VMDK
    assert parse_target("vpc").format is F.VHD
    with pytest.raises(ConvertError):
        parse_target("iso")


def test_nbd_server_serves_qemu(qemu, tmp_path):
    data = os.urandom(3 * MIB + 512)

    class Source:
        size = len(data)

        def read(self, offset, length):
            return data[offset:offset + length]

    with NbdServer(Source()) as server:
        spec = nbd_spec(server.socket_path, server.export)
        info = qemu.info(spec)
        assert info["virtual-size"] == len(data)
        out = tmp_path / "copy.raw"
        qemu.check_run(["convert", spec, "-O", "raw", str(out)], "test")
        assert out.read_bytes() == data
        same, _ = qemu.compare(spec, source_spec(out, "raw"))
        assert same


def test_nbd_read_errors_reach_qemu(qemu, tmp_path):
    class Broken:
        size = MIB

        def read(self, offset, length):
            raise OSError(5, "Input/output error")

    with NbdServer(Broken()) as server:
        spec = nbd_spec(server.socket_path, server.export)
        res = qemu.run(["convert", spec, "-O", "raw", str(tmp_path / "x.raw")])
        assert res.returncode != 0
        assert isinstance(server.error, OSError)


def test_cli_entry_point_runs(tmp_path):
    out = subprocess.run(["python3", "-m", "virtconvert", "--version"], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parent.parent)
    assert out.returncode == 0 and out.stdout.startswith("virtconvert ")


def test_unique_ids(qemu, vhdx_source):
    """Two conversions of one disk must not share an ID (VirtualBox refuses duplicate UUIDs)."""
    source, _ = vhdx_source
    _, a = run(qemu, source, Target(F.VDI), out=source.with_name("a.vdi"))
    _, b = run(qemu, source, Target(F.VDI), out=source.with_name("b.vdi"))
    assert probe(a.output).disk_id != probe(b.output).disk_id
    assert uuid.UUID(probe(a.output).disk_id)


def test_stale_log_guid_is_not_dirty(qemu, tmp_path):
    """A LogGuid without log entries is a clean file (qemu-img reads it too)."""
    write_vhdx(tmp_path / "stale.vhdx", 4 * MIB, {0: b"s" * SECTOR}, stale_log=True)
    info = probe(tmp_path / "stale.vhdx")
    assert info.ok and not info.dirty
    _, outcome = run(qemu, tmp_path / "stale.vhdx", parse_target("vdi"))
    assert as_raw(qemu, outcome.output, "vdi")[:SECTOR] == b"s" * SECTOR


def test_repair_applies_the_log(qemu, tmp_path):
    from virtconvert.core.convert import repair
    write_vhdx(tmp_path / "dirty.vhdx", 4 * MIB, {0: b"d" * SECTOR}, dirty=True)
    info = probe(tmp_path / "dirty.vhdx")
    assert info.dirty and not info.ok
    # qemu-img agrees that the log must be replayed first
    assert qemu.run(["info", source_spec(tmp_path / "dirty.vhdx", "vhdx")]).returncode != 0
    repair(info, qemu)
    after = probe(tmp_path / "dirty.vhdx")
    assert after.ok and not after.dirty
    _, outcome = run(qemu, tmp_path / "dirty.vhdx", parse_target("vdi"))
    assert as_raw(qemu, outcome.output, "vdi")[:SECTOR] == b"d" * SECTOR


def leftovers(folder: Path) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.endswith(".part")]


def test_nothing_is_left_behind(qemu, vhdx_source):
    source, _ = vhdx_source
    run(qemu, source, Target(F.VDI), out=source.with_name("ok.vdi"))
    plan = make_plan(Job(source=source, target=Target(F.VDI), output=source.with_name("c.vdi")), qemu)
    with pytest.raises(OperationCancelled):
        execute(plan, qemu, cancel=lambda: True)
    assert leftovers(source.parent) == []


def test_name_taken_while_converting_keeps_the_copy(qemu, vhdx_source):
    source, expected = vhdx_source
    out = source.with_name("race.vdi")
    plan = make_plan(Job(source=source, target=Target(F.VDI), output=out), qemu)

    def someone_else(stage, done, total):
        if stage == "Verifying" and done == total and not out.exists():
            out.write_bytes(b"theirs")

    with pytest.raises(ConvertError, match="was not replaced") as err:
        execute(plan, qemu, progress=someone_else)
    assert out.read_bytes() == b"theirs"
    kept = Path(str(err.value).rsplit("is in ", 1)[1].rstrip("."))
    assert kept.is_file() and as_raw(qemu, kept, "vdi") == expected


def test_vmdk_names_its_final_file(qemu, vhdx_source):
    source, _ = vhdx_source
    _, outcome = run(qemu, source, Target(F.VMDK), out=source.with_name("final name.vmdk"))
    head = outcome.output.read_bytes()[:64 * 1024]
    assert b'"final name.vmdk"' in head and b".part" not in head


def test_4k_sector_vhdx(qemu, tmp_path):
    size = 8 * MIB
    content = {s: bytes([s % 251]) * 4096 for s in range(0, size // 4096, 5)}
    write_vhdx(tmp_path / "4k.vhdx", size, content, logical=4096, block_size=MIB)
    plan, outcome = run(qemu, tmp_path / "4k.vhdx", Target(F.VDI))
    assert plan.uses_reader and any("4 KB sectors" in w for w in plan.warnings)
    expected = bytearray(size)
    for s, d in content.items():
        expected[s * 4096:(s + 1) * 4096] = d
    assert as_raw(qemu, outcome.output, "vdi") == bytes(expected)
    plan = make_plan(Job(source=tmp_path / "4k.vhdx", target=Target(F.VHDX), output=tmp_path / "o.vhdx"), qemu)
    assert not plan.ok and any("512-byte sectors" in e for e in plan.errors)


def test_old_qemu_cannot_grow_without_the_reader(qemu, tmp_path, monkeypatch):
    q = tmp_path / "d.qcow2"
    qemu.check_run(["create", "-q", "-f", "qcow2", str(q), str(MIB + 512)], "test")
    monkeypatch.setattr(qemu, "version", (4, 2, 1))
    plan = make_plan(Job(source=q, target=parse_target("azure"), output=tmp_path / "a.vhd"), qemu)
    assert not plan.ok and any("5.0" in e for e in plan.errors)


def test_replacing_is_announced(qemu, vhdx_source):
    source, _ = vhdx_source
    out = source.with_name("there.vdi")
    out.write_bytes(b"old")
    plan = make_plan(Job(source=source, target=Target(F.VDI), output=out, overwrite=True), qemu)
    assert plan.ok and any("will be replaced" in w for w in plan.warnings)


def test_nbd_socket_folder_is_short(monkeypatch, tmp_path):
    from virtconvert.core import nbd
    long_dir = tmp_path / ("x" * 90)
    long_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(long_dir))
    monkeypatch.setattr(nbd.tempfile, "gettempdir", lambda: str(long_dir))
    assert len(nbd._socket_parent()) < 70

    class Source:
        size = 4096

        def read(self, offset, length):
            return bytes(length)

    with NbdServer(Source()) as server:
        assert len(server.socket_path) < 108
