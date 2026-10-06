"""Planning and running a conversion.

A conversion writes `<output>.part` next to the output and renames it when
qemu-img has finished and (optionally) the copy has been compared with the
source, so an interrupted run never leaves a half-written file under the real
name. Sources are only ever read.
"""

from __future__ import annotations

import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import formats as F
from .chain import LAYERS, ChainReader
from .errors import ConvertError, OperationCancelled, VerifyError
from .formats import Format
from .images import inspect_image
from .nbd import NbdServer
from .probe import ImageInfo
from .qemu import QemuImg, nbd_spec, source_spec, spec
from .util import MIB, file_signature, human_bytes, round_up, same_file
from .vhdx import mark_regions_required

ProgressFn = Callable[[str, int, int], None]
CancelFn = Callable[[], bool]

STREAM_GRAIN = 64 * 1024

PRESETS = {
    "virtualbox": ("vdi", False, False, False),
    "vbox": ("vdi", False, False, False),
    "hyperv": ("vhdx", False, False, False),
    "hyper-v": ("vhdx", False, False, False),
    "vmware": ("vmdk", False, False, False),
    "ova": ("vmdk", False, True, False),
    "esxi": ("vmdk", False, True, False),
    "azure": ("vhd", True, False, True),
}


@dataclass(frozen=True)
class Target:
    format: Format
    fixed: bool = False
    stream: bool = False      # VMDK streamOptimized (OVA / ESXi)
    azure: bool = False       # VHD, fixed, size rounded up to whole MiB

    def normalized(self) -> "Target":
        fmt = self.format
        azure = self.azure and fmt is F.VHD
        stream = self.stream and fmt is F.VMDK
        fixed = (self.fixed or azure) and fmt.can_be_fixed and not stream
        return Target(fmt, fixed, stream, azure)

    @property
    def variant(self) -> str:
        if self.azure:
            return "Azure"
        if self.stream:
            return "stream-optimized"
        if self.format in (F.RAW,):
            return ""
        return "fixed" if self.fixed else "dynamic"

    @property
    def label(self) -> str:
        return f"{self.format.label} ({self.variant})" if self.variant else self.format.label

    def qemu_options(self) -> dict[str, str]:
        fmt = self.format
        if fmt is F.VDI:
            return {"static": "on"} if self.fixed else {}
        if fmt is F.VHD:
            # force_size keeps the exact size (no rounding to a CHS geometry), which
            # Hyper-V and Azure require
            return {"subformat": "fixed" if self.fixed else "dynamic", "force_size": "on"}
        if fmt is F.VHDX:
            return {"subformat": "fixed" if self.fixed else "dynamic"}
        if fmt is F.VMDK:
            return {"subformat": "streamOptimized" if self.stream else "monolithicSparse"}
        if fmt is F.QCOW2:
            return {"preallocation": "falloc"} if self.fixed else {}
        return {}


def parse_target(name: str, fixed: bool = False, stream: bool = False, azure: bool = False) -> Target:
    key = name.lower()
    if key in PRESETS:
        fmt_key, p_fixed, p_stream, p_azure = PRESETS[key]
        return Target(F.BY_KEY[fmt_key], fixed or p_fixed, stream or p_stream, azure or p_azure).normalized()
    fmt = F.by_name(key)
    if fmt is None:
        raise ConvertError(f"Unknown format '{name}'. Use one of: "
                           f"{', '.join(f.key for f in F.ALL)}, or {', '.join(sorted(set(PRESETS) - {'vbox', 'hyper-v'}))}.")
    return Target(fmt, fixed, stream, azure).normalized()


def default_target(info: ImageInfo | None) -> Target:
    return Target(F.default_target_for(info.format if info else None))


def unique_path(path: Path, taken: set[Path] | frozenset = frozenset()) -> Path:
    """PATH, or PATH with -2, -3… added if a file (or another planned output) has the name."""
    def free(p: Path) -> bool:
        return not p.exists() and p not in taken

    if free(path):
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if free(candidate):
            return candidate
    return path


def default_output(info: ImageInfo, target: Target, folder: Path | None = None, unique: bool = True,
                   taken: set[Path] | frozenset = frozenset()) -> Path:
    """Next to the source with the new extension. A checkpoint or snapshot is named
    after the disk it belongs to, since the result is that disk's current state."""
    source = info.path
    stem = source.stem
    directory = source.parent
    if info.chain:
        base = info.chain[-1].path
        stem = base.stem
        if source.parent.name.lower() == "snapshots":
            directory = source.parent.parent
    if folder is not None:
        directory = folder
    out = directory / f"{stem}{target.format.extension}"
    if same_file(out, source) or any(same_file(out, p.path) for p in info.chain):
        out = directory / f"{stem}-converted{target.format.extension}"
    if unique:
        return unique_path(out, taken)
    return out


@dataclass
class Job:
    source: Path
    target: Target
    output: Path
    verify: bool = True
    overwrite: bool = False
    parents: dict[Path, Path] = field(default_factory=dict)
    force_parent: bool = False
    candidates: list[Path] = field(default_factory=list)


@dataclass
class Plan:
    job: Job
    info: ImageInfo
    size: int = 0
    needed: int = 0
    free: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def target(self) -> Target:
        return self.job.target

    @property
    def uses_reader(self) -> bool:
        """VertConvert&Create reads the source itself: a chain, zeros added at the end, or a VHDX
        with 4 KB sectors (qemu-img's VHDX driver only handles 512-byte sectors)."""
        info = self.info
        return (info.needs_chain_reader or info.logical_sector != 512
                or (self.size != info.virtual_size and info.format in LAYERS))

    @property
    def grows_without_reader(self) -> bool:
        return self.size != self.info.virtual_size and not self.uses_reader


def make_plan(job: Job, qemu: QemuImg | None, info: ImageInfo | None = None) -> Plan:
    """Check everything that can be checked before converting."""
    job.target = job.target.normalized()
    if info is None:
        info = inspect_image(job.source, qemu, job.parents, job.force_parent, job.candidates)
    plan = Plan(job=job, info=info)
    target = job.target
    fmt = target.format
    plan.errors.extend(info.errors)
    plan.warnings.extend(info.warnings)
    plan.notes.extend(n for n in info.notes if not n.startswith("Comment:"))
    if info.format is None:
        return plan

    size = info.virtual_size
    if target.azure and size % MIB:
        size = round_up(size, MIB)
        plan.notes.append(f"Azure needs whole megabytes: the disk grows from {info.virtual_size:,} "
                          f"to {size:,} bytes (the guest sees a little free space at the end).")
    if target.stream and size % STREAM_GRAIN:
        if info.format in LAYERS:
            size = round_up(size, STREAM_GRAIN)
            plan.notes.append(f"Stream-optimized VMDK files hold whole 64 KB grains: the disk grows "
                              f"from {info.virtual_size:,} to {size:,} bytes.")
        else:
            plan.warnings.append("The disk size is not a multiple of 64 KB; VirtualBox and VMware may "
                                 "not read the last few KB of a stream-optimized VMDK.")
    plan.size = size
    if qemu is None:
        plan.errors.append("qemu-img is not installed. Run 'vertconvert doctor' to see how to install it.")
    else:
        if qemu.too_old:
            plan.errors.append(f"qemu-img {qemu.version_text} is too old; version 2.10 or newer is needed.")
        elif plan.grows_without_reader and qemu.version[:2] < (5, 0):
            plan.errors.append(f"Making this disk larger needs qemu-img 5.0 or newer (this one is "
                               f"{qemu.version_text}). Convert it to VDI or VHDX first, then to {target.label}.")
        needed_drivers = [fmt.driver]
        if plan.uses_reader:
            needed_drivers.append("nbd")
        else:
            needed_drivers.append(info.format.driver)
        for driver in qemu.missing_drivers(tuple(dict.fromkeys(needed_drivers))):
            plan.errors.append(f"The qemu-img on this system was built without the '{driver}' format.")

    if fmt.max_size and size > fmt.max_size:
        bigger = "VHDX" if fmt is F.VHD else "another format"
        plan.errors.append(f"{fmt.label} files can't hold more than {fmt.max_label} "
                           f"(this disk is {human_bytes(size)}); choose {bigger}.")

    # the output file
    out = job.output
    if not out.is_absolute():
        out = job.output = Path(os.path.abspath(out))
    sources = [info.path] + [p.path for p in info.chain] + info.extent_files
    if any(same_file(out, s) for s in sources):
        plan.errors.append("The output would overwrite the source. Choose another name.")
    elif out.exists() and not job.overwrite:
        plan.errors.append(f"{out.name} already exists. Choose another name or allow replacing files.")
    elif out.is_file() and job.overwrite:
        plan.warnings.append(f"{out.name} already exists and will be replaced.")
    elif out.exists() and out.is_dir():
        plan.errors.append(f"{out} is a folder.")
    if not out.parent.is_dir():
        plan.errors.append(f"The folder {out.parent} does not exist.")
    elif not os.access(out.parent, os.W_OK):
        plan.errors.append(f"You can't write to {out.parent}.")
    if out.suffix.lower() not in fmt.extensions and fmt is not F.RAW:
        plan.notes.append(f"The output name doesn't end in {fmt.extension}; hypervisors look at the extension.")

    # space
    plan.needed = size if target.fixed else min(size, max(info.stored_bytes, MIB))
    try:
        plan.free = shutil.disk_usage(out.parent).free if out.parent.is_dir() else None
    except OSError:
        plan.free = None
    if plan.free is not None and plan.needed > plan.free:
        plan.warnings.append(f"The converted disk may need up to {human_bytes(plan.needed)}, but only "
                             f"{human_bytes(plan.free)} is free on that drive.")

    _compatibility_notes(plan)
    return plan


def _compatibility_notes(plan: Plan) -> None:
    info, target = plan.info, plan.job.target
    fmt = target.format
    if info.format is fmt and not info.differencing and fmt is not F.RAW:
        same_kind = (info.subformat == "fixed") == target.fixed
        if same_kind:
            plan.notes.append(f"The source already is a {fmt.label} file; this makes a fresh copy "
                              "(unused space is left out).")
    if info.chain:
        count = len(info.chain) + 1
        plan.notes.append(f"A checkpoint of {info.chain[-1].name}: the {count} files are merged into "
                          "one complete disk (none of them is changed).")
    if info.logical_sector != 512:
        if fmt is F.VHDX:
            plan.errors.append("This disk uses 4 KB sectors, and qemu-img can only write VHDX files with "
                               "512-byte sectors: the guest wouldn't find its partitions in the copy. "
                               "Keep using the original file in Hyper-V.")
        else:
            plan.warnings.append("This disk uses 4 KB sectors. The converted disk has 512-byte sectors (the "
                                 "only kind VirtualBox and qemu-img offer), so a guest with partitions on it "
                                 "won't find them. Disks without a partition table are fine.")
    firmware = info.partitions.firmware if info.partitions else ""
    if fmt in (F.VDI, F.VMDK) and firmware == "uefi":
        plan.notes.append("The disk boots with UEFI: in VirtualBox turn on Settings → System → "
                          "Enable EFI for this VM.")
    if fmt is F.VHDX and firmware in ("uefi", "bios", "both"):
        gen = {"uefi": "generation 2", "bios": "generation 1", "both": "generation 1 or 2"}[firmware]
        plan.notes.append(f"The disk boots with {info.partitions.boot_label}: use a {gen} VM in Hyper-V.")
    if fmt is F.VHD and firmware == "uefi" and not target.azure:
        plan.warnings.append("The disk boots with UEFI, which needs a Hyper-V generation 2 VM, and those "
                             "only use VHDX. Choose VHDX for Hyper-V.")
    if target.stream:
        plan.notes.append("Stream-optimized VMDK files are for OVA appliances and VMware ESXi. "
                          "VirtualBox imports them but can't run a VM from one; use a regular VMDK for that.")
    if info.backing_file:
        plan.notes.append("qemu-img merges the image with its backing file.")


@dataclass
class Outcome:
    output: Path
    size: int
    file_size: int
    seconds: float
    verified: bool


def execute(plan: Plan, qemu: QemuImg, progress: ProgressFn | None = None,
            cancel: CancelFn | None = None, log: Callable[[str], None] | None = None) -> Outcome:
    if not plan.ok:
        raise ConvertError(plan.errors[0])
    job, info, target = plan.job, plan.info, plan.job.target
    progress = progress or (lambda *_: None)
    cancel = cancel or (lambda: False)
    log = log or (lambda _m: None)
    started = time.monotonic()
    out = job.output
    # Each run writes into its own folder next to the output, under the final file name
    # (a VMDK names its own file inside), and moves the file out when it is checked.
    work = out.parent / f"{out.name}.{secrets.token_hex(4)}.part"
    part = work / out.name
    keep_part = False
    watched = [info.path] + [p.path for p in info.chain] + info.extent_files
    signatures = {p: file_signature(p) for p in watched}

    def stage_progress(stage: str):
        return lambda pct: progress(stage, int(plan.size * min(pct, 100.0) / 100), plan.size)

    reader: ChainReader | None = None
    server: NbdServer | None = None
    grow = plan.size != info.virtual_size
    try:
        if plan.uses_reader:
            # VertConvert&Create reads the source (merging a chain, adding zeros at the end)
            # and qemu-img reads that through NBD
            reader = ChainReader.for_image(info, plan.size)
            server = NbdServer(reader).start()
            source = nbd_spec(server.socket_path, server.export)
            if info.chain:
                log(f"Merging {info.name} with {', '.join(p.name for p in info.chain)} while converting")
            grow = False
        else:
            source = source_spec(info.path, info.format.driver)
        work.mkdir()
        options = target.qemu_options()
        progress("Converting", 0, plan.size)
        log(f"qemu-img: {info.label} → {target.label}, {human_bytes(plan.size)}")
        try:
            if grow:
                # a larger target: create it at its size, then fill it
                qemu.create(part, target.format.driver, plan.size, options)
                qemu.convert(source, part, target.format.driver, options, existing_target=True,
                             progress=stage_progress("Converting"), cancel=cancel)
            else:
                qemu.convert(source, part, target.format.driver, options,
                             progress=stage_progress("Converting"), cancel=cancel)
        except ConvertError as exc:
            if server is not None and server.error is not None:
                raise ConvertError(f"Reading {info.name} failed: {server.error}") from exc
            raise
        progress("Converting", plan.size, plan.size)
        if target.format is F.VHDX:
            mark_regions_required(part)
        _fsync(part)
        _check_unchanged(signatures)
        verified = False
        if job.verify:
            if cancel():
                raise OperationCancelled()
            progress("Verifying", 0, plan.size)
            log("Comparing the copy with the source")
            same, detail = qemu.compare(source, source_spec(part, target.format.driver),
                                        progress=stage_progress("Verifying"), cancel=cancel)
            if server is not None and server.error is not None:
                raise ConvertError(f"Reading {info.name} failed: {server.error}")
            if not same:
                raise VerifyError(f"The converted disk differs from the source: {detail}")
            _check_unchanged(signatures)
            verified = True
            progress("Verifying", plan.size, plan.size)
        try:
            _publish(part, out, job.overwrite)
        except FileExistsError:
            keep_part = True
            raise ConvertError(f"{out.name} appeared while converting, so it was not replaced. The "
                               f"converted copy is in {part}.") from None
        shutil.rmtree(work, ignore_errors=True)
        return Outcome(output=out, size=plan.size, file_size=out.stat().st_size,
                       seconds=time.monotonic() - started, verified=verified)
    except BaseException:
        if not keep_part:
            shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        if server is not None:
            server.stop()
        if reader is not None:
            reader.close()


def _publish(part: Path, out: Path, overwrite: bool) -> None:
    """Give the checked copy its real name. Without OVERWRITE an existing file is never
    replaced, not even one that appeared a moment ago (FileExistsError)."""
    if overwrite:
        os.replace(part, out)
        return
    try:
        os.link(part, out)      # atomic, and fails if OUT exists
    except FileExistsError:
        raise
    except OSError:
        # no hard links on this filesystem (FAT, exFAT, some network shares)
        if out.exists():
            raise FileExistsError(out) from None
        os.replace(part, out)
    else:
        part.unlink()


def _fsync(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _check_unchanged(signatures: dict[Path, tuple[int, int] | None]) -> None:
    for path, before in signatures.items():
        if before is not None and file_signature(path) != before:
            raise ConvertError(f"{path.name} changed while it was being converted (is the virtual "
                               "machine running?). Shut it down and convert again.")


def verify_images(a: ImageInfo, b: ImageInfo, qemu: QemuImg, progress: ProgressFn | None = None,
                  cancel: CancelFn | None = None) -> tuple[bool, str]:
    """Compare what two images contain (any formats, differencing images included)."""
    progress = progress or (lambda *_: None)
    total = max(a.virtual_size, b.virtual_size)
    readers: list = []
    servers: list[NbdServer] = []
    try:
        specs = []
        for info in (a, b):
            if info.needs_chain_reader:
                reader = ChainReader.for_image(info)
                readers.append(reader)
                server = NbdServer(reader).start()
                servers.append(server)
                specs.append(nbd_spec(server.socket_path, server.export))
            else:
                specs.append(source_spec(info.path, info.format.driver))
        return qemu.compare(specs[0], specs[1], cancel=cancel,
                            progress=lambda pct: progress("Comparing", int(total * pct / 100), total))
    finally:
        for server in servers:
            server.stop()
        for reader in readers:
            reader.close()


def repair(info: ImageInfo, qemu: QemuImg) -> str:
    """Apply a VHDX log (what Hyper-V does when it opens the file). Changes the file."""
    res = qemu.check(info.path, info.format.driver, repair=True)
    # 0: fine now, 3: only leaked clusters (harmless); anything else did not work
    if res.returncode not in (0, 3):
        raise ConvertError(f"Repair failed: {res.stderr.strip() or res.stdout.strip()}")
    text = (res.stdout + res.stderr).strip()
    return text or "Repaired."


__all__ = ["Target", "Job", "Plan", "Outcome", "PRESETS", "parse_target", "default_target",
           "default_output", "unique_path", "make_plan", "execute", "verify_images", "repair", "spec"]
