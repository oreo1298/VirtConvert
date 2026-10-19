"""Planning and running a conversion.

A conversion writes into its own folder `<output>.<random>.part` next to the
output and moves the files out when qemu-img has finished and (optionally) the
copy has been compared with the source, so an interrupted run never leaves a
half-written file under the real name. Sources are only ever read.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import tarfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import devices, ova
from . import formats as F
from .chain import LAYERS, ChainReader
from .errors import ConvertError, OperationCancelled, VerifyError
from .formats import Format
from .images import inspect_image
from .nbd import NbdServer
from .probe import ImageInfo, probe
from .qemu import QemuImg, image_spec, member_spec, nbd_spec, source_spec, spec
from .util import MIB, file_signature, human_bytes, round_up, same_file
from .vhdx import mark_regions_required

ProgressFn = Callable[[str, int, int], None]
CancelFn = Callable[[], bool]

STREAM_GRAIN = 64 * 1024

PRESETS = {
    # name: (format, fixed, stream, azure, split)
    "virtualbox": ("vdi", False, False, False, False),
    "vbox": ("vdi", False, False, False, False),
    "hyperv": ("vhdx", False, False, False, False),
    "hyper-v": ("vhdx", False, False, False, False),
    "vmware": ("vmdk", False, False, False, False),
    "esxi": ("vmdk", False, True, False, False),
    "stream": ("vmdk", False, True, False, False),
    "azure": ("vhd", True, False, True, False),
}
VMDK_SPLIT = 2 << 30


@dataclass(frozen=True)
class Target:
    format: Format
    fixed: bool = False
    stream: bool = False      # VMDK streamOptimized (ESXi; also what an OVA holds)
    azure: bool = False       # VHD, fixed, size rounded up to whole MiB
    split: bool = False       # VMDK in 2 GB files (VMware Workstation's default)

    def normalized(self) -> "Target":
        fmt = self.format
        azure = self.azure and fmt is F.VHD
        stream = self.stream and fmt is F.VMDK
        split = self.split and fmt is F.VMDK and not stream
        fixed = (self.fixed or azure) and fmt.can_be_fixed and not stream
        return Target(fmt, fixed, stream, azure, split)

    @property
    def variant(self) -> str:
        fmt = self.format
        if self.azure:
            return "Azure"
        if fmt is F.OVA:
            return "appliance"
        if fmt is F.VMDK:
            kind = "stream-optimized" if self.stream else "preallocated" if self.fixed else "dynamic"
            return f"{kind}, 2 GB files" if self.split else kind
        if fmt in (F.RAW, F.QED):
            return "preallocated" if self.fixed and fmt is F.RAW else ""
        return "fixed" if self.fixed else "dynamic"

    @property
    def label(self) -> str:
        return f"{self.format.label} ({self.variant})" if self.variant else self.format.label

    @property
    def vmdk_subformat(self) -> str:
        if self.stream or self.format is F.OVA:
            return "streamOptimized"
        if self.split:
            return "twoGbMaxExtentFlat" if self.fixed else "twoGbMaxExtentSparse"
        return "monolithicFlat" if self.fixed else "monolithicSparse"

    @property
    def multi_file(self) -> bool:
        return self.format is F.VMDK and (self.split or self.fixed)

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
        if fmt in (F.VMDK, F.OVA):
            return {"subformat": self.vmdk_subformat}
        if fmt in (F.QCOW2, F.RAW):
            return {"preallocation": "falloc"} if self.fixed else {}
        return {}

    def output_files(self, out: Path, size: int) -> list[Path]:
        """Every file the target consists of (a VMDK can come in several, named the way
        qemu-img names them: the descriptor's name with -flat, -s001, -f001… added)."""
        if not self.multi_file:
            return [out]
        # like qemu-img: everything before the last dot, and the rest ("disk." -> "disk-flat.")
        dot = out.name.rfind(".")
        stem, ext = (out.name[:dot], out.name[dot:]) if dot >= 0 else (out.name, "")
        if not self.split:
            return [out, out.with_name(f"{stem}-flat{ext}")]
        letter = "f" if self.fixed else "s"
        count = max(1, -(-size // VMDK_SPLIT))
        return [out] + [out.with_name(f"{stem}-{letter}{n:03d}{ext}") for n in range(1, count + 1)]


def parse_target(name: str, fixed: bool = False, stream: bool = False, azure: bool = False,
                 split: bool = False) -> Target:
    key = name.lower()
    if key in PRESETS:
        fmt_key, p_fixed, p_stream, p_azure, p_split = PRESETS[key]
        return Target(F.BY_KEY[fmt_key], fixed or p_fixed, stream or p_stream, azure or p_azure,
                      split or p_split).normalized()
    fmt = F.by_name(key)
    if fmt is F.QCOW:
        fmt = F.QCOW2        # nobody wants to write the old version; "qcow" meant QCOW2 before 1.1
    if fmt is None:
        raise ConvertError(f"Unknown format '{name}'. Use one of: "
                           f"{', '.join(f.key for f in F.WRITABLE)}, or "
                           f"{', '.join(sorted(set(PRESETS) - {'vbox', 'hyper-v'}))}.")
    if not fmt.writable:
        raise ConvertError(f"{fmt.label} can be read but not written. Choose one of: "
                           f"{', '.join(f.key for f in F.WRITABLE)}.")
    return Target(fmt, fixed, stream, azure, split).normalized()


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
    stem = info.stem
    directory = source.parent
    if info.device:      # not next to /dev/sdb: in the current folder, or the home folder
        directory = Path.cwd() if os.access(Path.cwd(), os.W_OK) else Path.home()
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
    member: str | None = None        # a disk inside the OVA SOURCE
    elevate: str = "auto"            # how to read a physical disk: pkexec, sudo, auto (devices.elevator)
    partitions: tuple[int, ...] = ()     # only these partitions of the physical disk SOURCE


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
    can_verify: bool = True          # false for a physical disk in use: it changes while it's read

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def target(self) -> Target:
        return self.job.target

    @property
    def uses_reader(self) -> bool:
        return reads_itself(self.info, self.size)

    @property
    def grows_without_reader(self) -> bool:
        return self.size != self.info.virtual_size and not self.uses_reader


def reads_itself(info: ImageInfo, size: int) -> bool:
    """VirtConvert&Create reads the source itself (qemu-img reads that through NBD): a physical
    disk, a chain, zeros added at the end (SIZE beyond the disk's), or a VHDX with 4 KB sectors
    (qemu-img's VHDX driver only handles 512-byte sectors)."""
    if info.device:
        return True         # a physical disk: read through the helper's file descriptor
    if info.member:
        return False        # a disk inside an OVA: qemu-img reads it in place
    return (info.needs_chain_reader or info.logical_sector != 512
            or (size != info.virtual_size and info.format in LAYERS))


def missing_qemu(qemu: QemuImg | None, drivers: list[str]) -> list[str]:
    """What keeps QEMU from doing the work: not installed, too old, or built without DRIVERS."""
    if qemu is None:
        return ["qemu-img is not installed. Run 'virtconvert doctor' to see how to install it."]
    if qemu.too_old:
        return [f"qemu-img {qemu.version_text} is too old; version 2.10 or newer is needed."]
    return [f"The qemu-img on this system was built without the '{driver}' format."
            for driver in qemu.missing_drivers(tuple(dict.fromkeys(drivers)))]


def make_plan(job: Job, qemu: QemuImg | None, info: ImageInfo | None = None) -> Plan:
    """Check everything that can be checked before converting."""
    job.target = job.target.normalized()
    if info is None:
        info = inspect_image(job.source, qemu, job.parents, job.force_parent, job.candidates, job.member,
                             job.partitions)
    plan = Plan(job=job, info=info)
    target = job.target
    fmt = target.format
    plan.errors.extend(info.errors)
    plan.warnings.extend(info.warnings)
    plan.notes.extend(n for n in info.notes if not n.startswith("Comment:"))
    if info.format is None:
        return plan
    if not fmt.writable:
        plan.errors.append(f"{fmt.label} can be read but not written.")

    size = info.virtual_size
    if target.azure and size % MIB:
        size = round_up(size, MIB)
        plan.notes.append(f"Azure needs whole megabytes: the disk grows from {info.virtual_size:,} "
                          f"to {size:,} bytes (the guest sees a little free space at the end).")
    if (target.stream or fmt is F.OVA) and size % STREAM_GRAIN:
        if info.format in LAYERS and not info.member:
            size = round_up(size, STREAM_GRAIN)
            plan.notes.append(f"Stream-optimized VMDK files hold whole 64 KB grains: the disk grows "
                              f"from {info.virtual_size:,} to {size:,} bytes.")
        else:
            plan.warnings.append("The disk size is not a multiple of 64 KB; VirtualBox and VMware may "
                                 "not read the last few KB of a stream-optimized VMDK.")
    plan.size = size
    plan.errors += missing_qemu(qemu, [fmt.driver, "nbd" if plan.uses_reader else info.format.driver])
    if (qemu is not None and not qemu.too_old and plan.grows_without_reader and qemu.version[:2] < (5, 0)):
        plan.errors.append(f"Making this disk larger needs qemu-img 5.0 or newer (this one is "
                           f"{qemu.version_text}). Convert it to VDI or VHDX first, then to {target.label}.")

    if fmt.max_size and size > fmt.max_size:
        bigger = "VHDX" if fmt is F.VHD else "another format"
        plan.errors.append(f"{fmt.label} files can't hold more than {fmt.max_label} "
                           f"(this disk is {human_bytes(size)}); choose {bigger}.")

    # the output file
    if not job.output.is_absolute():
        job.output = Path(os.path.abspath(job.output))
    sources = [info.path] + [p.path for p in info.chain] + info.extent_files
    check_output(plan, job.output, target, size, job.overwrite, sources)
    if info.device:
        _device_checks(plan)

    # space (an appliance is packed from a converted disk, which needs the room twice for a moment)
    check_space(plan, job.output, size if target.fixed else min(size, max(info.stored_bytes, MIB)), fmt)

    _compatibility_notes(plan)
    return plan


def check_output(plan, out: Path, target: Target, size: int, overwrite: bool, sources: list[Path]) -> None:
    """Problems with writing TARGET as OUT (and the files that go with it) go into PLAN."""
    fmt = target.format
    outputs = target.output_files(out, size)
    if devices.is_device(out):
        plan.errors.append(f"{out} is a disk, not a file. To write a virtual disk onto a physical one, use "
                           "Write to disk in the app, or 'virtconvert write'.")
    elif any(same_file(o, s) for o in outputs for s in sources):
        plan.errors.append("The output would overwrite the source. Choose another name.")
    elif out.exists() and out.is_dir():
        plan.errors.append(f"{out} is a folder.")
    else:
        existing = [o.name for o in outputs if o.exists()]
        if existing and not overwrite:
            plan.errors.append(f"{', '.join(existing)} already exist{'s' if len(existing) == 1 else ''}. "
                               "Choose another name or allow replacing files.")
        elif existing:
            plan.warnings.append(f"{', '.join(existing)} already exist{'s' if len(existing) == 1 else ''} "
                                 "and will be replaced.")
        stale = _old_extents(out, {o.name for o in outputs}) if overwrite else []
        if stale:
            plan.warnings.append(f"The replaced {out.name}'s other data files ({', '.join(p.name for p in stale)}) "
                                 "are removed too.")
    if not out.parent.is_dir():
        plan.errors.append(f"The folder {out.parent} does not exist.")
    elif not os.access(out.parent, os.W_OK):
        plan.errors.append(f"You can't write to {out.parent}.")
    if out.suffix.lower() not in fmt.extensions and fmt is not F.RAW:
        plan.notes.append(f"The output name doesn't end in {fmt.extension}; hypervisors look at the extension.")


def check_space(plan, out: Path, needed: int, fmt: Format) -> None:
    """Warn in PLAN when OUT's drive has less than NEEDED bytes free (twice that for an OVA,
    which is packed from a disk made first)."""
    plan.needed = needed * 2 if fmt is F.OVA else needed
    try:
        plan.free = shutil.disk_usage(out.parent).free if out.parent.is_dir() else None
    except OSError:
        plan.free = None
    if plan.free is not None and plan.needed > plan.free:
        plan.warnings.append(f"The disk may need up to {human_bytes(plan.needed)}, but only "
                             f"{human_bytes(plan.free)} is free on that drive.")


def _device_checks(plan: Plan) -> None:
    """A physical disk: never save its copy on what is being read, and tell what being in use means."""
    info, out = plan.info, plan.job.output
    if info.layout is not None:
        # some partitions: the copy may go onto another partition of the same disk
        same = devices.partitions_holding(out.parent) & {p.part.name for p in info.layout.placements}
        if same:
            plan.errors.append(f"{out.parent} is on {', '.join(sorted(same))}, a partition being copied. Save the "
                               "copy on another partition or drive.")
    else:
        same = devices.disks_holding(out.parent) & devices.base_disks(info.path)
        if same:
            plan.errors.append(f"{out.parent} is on the disk being copied ({', '.join(sorted(same))}). Save the "
                               "copy on another drive.")
    if info.mountpoints:
        plan.can_verify = False
        shown = ", ".join(info.mountpoints[:4]) + ("…" if len(info.mountpoints) > 4 else "")
        if any(m in devices.SYSTEM_MOUNTS for m in info.mountpoints):
            plan.warnings.append(f"This disk holds the running system (in use at {shown}). Its files change "
                                 "while it is copied, so the copy may be inconsistent, and it can't be compared "
                                 "with the disk afterwards. For a reliable copy, start the computer from a live "
                                 "USB system and copy the disk from there.")
        else:
            plan.warnings.append(f"The disk is in use (mounted at {shown}). Unmount it first for a consistent "
                                 "copy; while it is mounted, the copy can't be compared with the disk afterwards.")
    if not os.access(info.path, os.R_OK):
        plan.notes.append("Reading a disk needs administrator rights: you'll be asked for your password.")
    plan.notes.append("The disk is only read, never written to.")
    if info.layout is not None and _starts_a_system(info):
        plan.notes.append("A system on the copied partitions moves with them, and the boot code at the start of the "
                          "disk isn't copied: to start it in a virtual machine, include its EFI system partition "
                          "(UEFI), or reinstall its boot loader there (BIOS: grub-install; Windows: bcdboot). For a "
                          "copy that starts as it is, copy the whole disk.")
    if guess_os(info) == "windows":
        plan.notes.append("Windows: shut it down completely before copying (Fast Startup leaves the disk "
                          "half-saved: hold Shift while clicking Shut down), and keep the BitLocker recovery key "
                          "at hand if the disk is encrypted: the virtual machine will ask for it.")


def _starts_a_system(info: ImageInfo) -> bool:
    """Whether the partitions look like what a computer starts from (not just a data partition)."""
    parts = [p.part for p in info.layout.placements] if info.layout is not None else []
    return any(p.kind in ("EFI system", "BIOS boot", "Microsoft reserved", "Windows recovery")
               or p.kind.startswith("Linux root") or p.bootable for p in parts)


def _compatibility_notes(plan: Plan) -> None:
    info, target = plan.info, plan.job.target
    fmt = target.format
    if info.format is fmt and not info.differencing and fmt is not F.RAW and not info.device:
        same_kind = (info.subformat == "fixed") == target.fixed
        if same_kind:
            plan.notes.append(f"The source already is a {fmt.label} file; this makes a fresh copy "
                              "(unused space is left out).")
    if info.chain:
        count = len(info.chain) + 1
        plan.notes.append(f"A checkpoint of {info.chain[-1].name}: the {count} files are merged into "
                          "one complete disk (none of them is changed).")
    if info.logical_sector != 512:
        if info.device:
            plan.warnings.append("This disk uses 4 KB sectors, and virtual disks have 512-byte sectors: a "
                                 "system on it won't find its partitions in the copy. Disks without a partition "
                                 "table are fine.")
        elif info.member:
            plan.errors.append("This disk uses 4 KB sectors, which can't be read inside the archive. "
                               f"Unpack {info.member} from {info.path.name} first (tar -xf) and convert that.")
        elif fmt is F.VHDX:
            plan.errors.append("This disk uses 4 KB sectors, and qemu-img can only write VHDX files with "
                               "512-byte sectors: the guest wouldn't find its partitions in the copy. "
                               "Keep using the original file in Hyper-V.")
        else:
            plan.warnings.append("This disk uses 4 KB sectors. The converted disk has 512-byte sectors (the "
                                 "only kind VirtualBox and qemu-img offer), so a guest with partitions on it "
                                 "won't find them. Disks without a partition table are fine.")
    firmware = info.partitions.firmware if info.partitions else ""
    if fmt in (F.VDI, F.VMDK, F.QED) and firmware == "uefi":
        plan.notes.append("The disk boots with UEFI: in VirtualBox turn on Settings → System → "
                          "Enable EFI for this VM.")
    if fmt is F.OVA:
        plan.notes.append("Import it with File → Import Appliance (VirtualBox, VMware). The machine gets 2 CPUs, "
                          f"{'4' if guess_os(info) == 'windows' else '2'} GB of memory, a SATA disk and a NAT "
                          "network card; adjust them while importing."
                          + (" The disk boots with UEFI: in VirtualBox turn on Enable EFI after importing "
                             "(VMware picks it up by itself)." if firmware == "uefi" else ""))
    if fmt is F.QCOW2 and firmware == "uefi":
        plan.notes.append("The disk boots with UEFI: choose UEFI (OVMF) firmware for the VM in virt-manager "
                          "or Proxmox.")
    if fmt is F.VHDX and firmware in ("uefi", "bios", "both"):
        gen = {"uefi": "generation 2", "bios": "generation 1", "both": "generation 1 or 2"}[firmware]
        plan.notes.append(f"The disk boots with {info.partitions.boot_label}: use a {gen} VM in Hyper-V.")
    if fmt is F.VHD and firmware == "uefi" and not target.azure:
        plan.warnings.append("The disk boots with UEFI, which needs a Hyper-V generation 2 VM, and those "
                             "only use VHDX. Choose VHDX for Hyper-V.")
    if target.stream:
        plan.notes.append("Stream-optimized VMDK files are for VMware ESXi and appliances. "
                          "VirtualBox imports them but can't run a VM from one; use a regular VMDK for that.")
    if target.multi_file:
        files = target.output_files(plan.job.output, plan.size)
        plan.notes.append(f"The VMDK consists of {len(files)} files ({files[0].name} and "
                          f"{files[1].name}{'…' if len(files) > 2 else ''}); keep them together.")
    if info.backing_file:
        plan.notes.append("qemu-img merges the image with its backing file.")


def guess_os(info: ImageInfo) -> str:
    """'windows', 'linux' or '' from the partitions on the disk."""
    kinds = {p.kind for p in info.partitions.partitions} if info.partitions else set()
    if kinds & {"Basic data", "Microsoft reserved", "Windows recovery", "NTFS/exFAT", "Windows LDM data"}:
        return "windows"
    if any(k.startswith("Linux") for k in kinds):
        return "linux"
    return ""


@dataclass
class Outcome:
    output: Path
    size: int
    file_size: int
    seconds: float
    verified: bool
    files: list[Path] = field(default_factory=list)    # every file written (a VMDK can have several)


def execute(plan: Plan, qemu: QemuImg, progress: ProgressFn | None = None,
            cancel: CancelFn | None = None, log: Callable[[str], None] | None = None) -> Outcome:
    if not plan.ok:
        raise ConvertError(plan.errors[0])
    job, info, target = plan.job, plan.info, plan.job.target
    log = log or (lambda _m: None)
    signatures = {p: file_signature(p) for p in source_files(info)}
    work = work_folder(job.output)
    with open_source(info, plan.size, plan.uses_reader, job.elevate, cancel, log) as (source, source_error):
        log(f"qemu-img: {info.label} → {target.label}, {human_bytes(plan.size)}")
        appliance = Appliance(ova.appliance_name(info.stem),
                              info.partitions.firmware if info.partitions else "", guess_os(info))
        work.mkdir()        # write_image removes it
        if job.verify and not plan.can_verify:
            log("Not comparing the copy with the disk: it is in use and keeps changing")
        return write_image(
            qemu, source, plan.size, target, job.output, work, verify=job.verify and plan.can_verify,
            overwrite=job.overwrite,
            grow=plan.size != info.virtual_size and not plan.uses_reader, appliance=appliance,
            check=lambda: check_unchanged(signatures), source_error=source_error,
            progress=progress, cancel=cancel, log=log)


def source_files(info: ImageInfo) -> list[Path]:
    """The files the image consists of (none for a physical disk), to notice changes while copying."""
    return [] if info.device else [info.path] + [p.path for p in info.chain] + info.extent_files


@contextmanager
def open_source(info: ImageInfo, size: int, through_reader: bool, elevate: str = "auto",
                cancel: CancelFn | None = None,
                log: Callable[[str], None] | None = None) -> Iterator[tuple[str, Callable[[], str | None]]]:
    """The qemu-img specification to read the disk INFO (SIZE bytes) from, and a function that
    tells why reading failed, if it was VirtConvert&Create's reader. A physical disk is opened
    read-only (by a helper with administrator rights if needed) and served through NBD like a
    chain the reader merges (THROUGH_READER)."""
    log = log or (lambda _m: None)
    reader: ChainReader | devices.DeviceReader | None = None
    server: NbdServer | None = None
    try:
        if info.device:
            log(f"Opening {info.path} for reading")
            fd = devices.open_device(info.path, elevate, cancel)
            try:
                disk = devices.DeviceReader(fd, info.layout.disk.size if info.layout is not None else size, info.name)
            except BaseException:
                os.close(fd)
                raise
            if info.layout is not None:
                # some partitions: a new partition table, and their bytes where they are on the disk
                try:
                    planned = info.layout.disk
                    if planned.seq and devices.disk_info(info.path).seq != planned.seq:
                        raise ConvertError(f"{info.path} is not the disk that was chosen any more (was it "
                                           "unplugged, and another one plugged in?). Choose it again.")
                    reader = devices.PartitionReader(disk, info.layout, size)
                except BaseException:
                    disk.close()
                    raise
                log(f"Reading only {info.layout.names}")
            else:
                reader = disk
            server = NbdServer(reader).start()
            source = nbd_spec(server.socket_path, server.export)
        elif through_reader:
            # VirtConvert&Create reads the source (merging a chain, adding zeros at the end)
            # and qemu-img reads that through NBD
            reader = ChainReader.for_image(info, size)
            server = NbdServer(reader).start()
            source = nbd_spec(server.socket_path, server.export)
            if info.chain:
                log(f"Merging {info.name} with {', '.join(p.name for p in info.chain)} while converting")
        else:
            source = image_spec(info)

        def source_error() -> str | None:
            if server is not None and server.error is not None:
                return f"Reading {info.name} failed: {server.error}"
            return None

        yield source, source_error
    finally:
        if server is not None:
            server.stop()
        if reader is not None:
            reader.close()


def work_folder(out: Path) -> Path:
    """Where a run writes: its own folder next to the output, so the files get their final
    names inside (a VMDK refers to its other files by name) and move out when checked."""
    return out.parent / f"{out.name}.{secrets.token_hex(4)}.part"


@dataclass(frozen=True)
class Appliance:
    """What an OVA says about the machine around its disk."""
    name: str = "disk"
    firmware: str = ""       # "uefi" asks for EFI
    os_hint: str = ""        # "windows", "linux"


def write_image(qemu: QemuImg, source: str | None, size: int, target: Target, out: Path, work: Path, *,
                verify: bool = True, overwrite: bool = False, grow: bool = False,
                appliance: Appliance = Appliance(), check: Callable[[], None] = lambda: None,
                source_error: Callable[[], str | None] = lambda: None,
                progress: ProgressFn | None = None, cancel: CancelFn | None = None,
                log: Callable[[str], None] | None = None, stage: str = "Converting",
                compare: Callable[[Path, Callable[[float], None], CancelFn], tuple[bool, str]] | None = None,
                extra_options: dict[str, str] | None = None) -> Outcome:
    """Write the qemu-img SOURCE (SIZE bytes; None: an empty disk) as TARGET into the existing
    folder WORK, compare it with the source, and give it its name OUT. Files of WORK's subfolders are
    left alone. WORK is removed afterwards, also on failure, except when OUT's name was
    taken meanwhile (the checked copy is kept and its place reported).

    COMPARE(written file, percent callback, cancel) replaces qemu-img compare; EXTRA_OPTIONS
    are added to the target's qemu-img options."""
    fmt = target.format
    progress = progress or (lambda *_: None)
    cancel = cancel or (lambda: False)
    log = log or (lambda _m: None)
    started = time.monotonic()
    part = work / out.name
    keep = False

    def stage_progress(stage: str):
        return lambda pct: progress(stage, int(size * min(pct, 100.0) / 100), size)

    def failed_source(exc: Exception | None = None) -> None:
        message = source_error()
        if message:
            raise ConvertError(message) from exc

    try:
        options = {**target.qemu_options(), **(extra_options or {})}
        # an appliance is packed from a stream-optimized VMDK made first
        disk = work / "appliance" / f"{appliance.name}-disk1.vmdk" if fmt is F.OVA else part
        disk.parent.mkdir(exist_ok=True)
        progress(stage, 0, size)
        try:
            if source is None:
                qemu.create(disk, fmt.driver, size, options)
            elif grow:
                # a larger target: create it at its size, then fill it
                qemu.create(disk, fmt.driver, size, options)
                qemu.convert(source, disk, fmt.driver, options, existing_target=True,
                             progress=stage_progress(stage), cancel=cancel)
            else:
                qemu.convert(source, disk, fmt.driver, options, progress=stage_progress(stage), cancel=cancel)
        except ConvertError as exc:
            failed_source(exc)
            raise
        progress(stage, size, size)
        if fmt is F.VHDX:
            mark_regions_required(part)
        if fmt is F.OVA:
            if cancel():
                raise OperationCancelled()
            log(f"Packing the appliance {appliance.name} (2 CPUs, SATA disk, NAT network)")
            progress("Packing", 0, 0)
            try:
                ova.write_ova(part, disk, appliance.name, size, appliance.firmware, appliance.os_hint,
                              progress=lambda done, total: progress("Packing", done, total), cancel=cancel)
            except (OSError, ValueError, tarfile.TarError) as exc:
                raise ConvertError(f"Packing the appliance failed: {getattr(exc, 'strerror', None) or exc}") from None
            shutil.rmtree(disk.parent)
            packed = ova.list_ova(part)[0]
            copy = member_spec(part, "vmdk", packed.offset, packed.size)
        else:
            copy = source_spec(part, fmt.driver)
        files = sorted(p for p in work.iterdir() if p.is_file())
        for f in files:
            _fsync(f)
        check()
        verified = False
        if verify:
            if cancel():
                raise OperationCancelled()
            progress("Verifying", 0, size)
            if source is None:
                log("Checking the new disk")
                same, detail = _check_empty(part, fmt, size)
            elif compare is not None and fmt is not F.OVA:
                log("Comparing the new disk with what was written")
                same, detail = compare(part, stage_progress("Verifying"), cancel)
            else:
                log("Comparing the copy with the source")
                same, detail = qemu.compare(source, copy, progress=stage_progress("Verifying"), cancel=cancel)
            failed_source()
            if not same:
                raise VerifyError(f"The new disk isn't right: {detail}" if source is None else
                                  f"The converted disk differs from the source: {detail}")
            check()
            verified = True
            progress("Verifying", size, size)
        # the descriptor last, so a VMDK appears only when its data files are in place
        files.sort(key=lambda f: f == part)
        stale = _old_extents(out, {f.name for f in files}) if overwrite else []
        try:
            _publish(files, out.parent, overwrite)
        except FileExistsError as exc:
            keep = True
            for sub in (p for p in work.iterdir() if p.is_dir()):
                shutil.rmtree(sub, ignore_errors=True)
            kept = part if len(files) == 1 else work
            raise ConvertError(f"{Path(str(exc.args[0])).name} appeared while converting, so it was not "
                               f"replaced. The converted copy is in {kept}.") from None
        for old in stale:       # data files of the VMDK that was replaced, which the new one doesn't use
            old.unlink(missing_ok=True)
            log(f"Removed {old.name}, a data file of the replaced {out.name}")
        published = [out.parent / f.name for f in sorted(files, key=lambda f: (f != part, f.name))]
        return Outcome(output=out, size=size, file_size=sum(f.stat().st_size for f in published),
                       seconds=time.monotonic() - started, verified=verified, files=published)
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def _old_extents(out: Path, new_names: set[str]) -> list[Path]:
    """The data files of the VMDK OUT that a new OUT replaces and doesn't use itself: only the
    ones next to it and named the way OUT's own files are (out-flat.vmdk, out-s001.vmdk…)."""
    try:
        if not out.is_file() or out.stat().st_size > (1 << 20):
            return []
        old = probe(out)
    except OSError:
        return []
    if old.format is not F.VMDK:
        return []
    dot = out.name.rfind(".")
    stem = out.name[:dot] if dot > 0 else out.name
    ours = re.compile(re.escape(stem) + r"-(flat|[sf]\d{3})" + re.escape(out.name[dot:] if dot > 0 else ""))
    return [p for p in old.extent_files
            if p.parent == out.parent and p.name not in new_names and ours.fullmatch(p.name) and p.exists()]


def _check_empty(path: Path, fmt: Format, size: int) -> tuple[bool, str]:
    """An empty new disk: it opens as FMT and has SIZE bytes."""
    if fmt is F.RAW:
        actual = path.stat().st_size
        return actual == size, f"the file has {actual:,} bytes instead of {size:,}"
    info = probe(path)
    if not info.ok:
        return False, "; ".join(info.errors)
    if info.format is not (F.VMDK if fmt is F.OVA else fmt):
        return False, f"it reads as {info.label}"
    if info.virtual_size != size:
        return False, f"it holds {info.virtual_size:,} bytes instead of {size:,}"
    return True, "The new disk opens and has the right size."


def _publish(files: list[Path], folder: Path, overwrite: bool) -> None:
    """Move the checked FILES into FOLDER. Without OVERWRITE an existing file is never
    replaced, not even one that appeared a moment ago: FileExistsError(path), and the
    files placed so far are taken back."""
    if overwrite:
        for f in files:
            os.replace(f, folder / f.name)
        return
    placed: list[tuple[Path, Path, bool]] = []     # (file, destination, moved instead of linked)
    try:
        for f in files:
            dest = folder / f.name
            try:
                os.link(f, dest)      # atomic, and fails if DEST exists
                placed.append((f, dest, False))
            except FileExistsError:
                raise FileExistsError(str(dest)) from None
            except OSError:
                # no hard links on this filesystem (FAT, exFAT, some network shares)
                if dest.exists():
                    raise FileExistsError(str(dest)) from None
                os.replace(f, dest)
                placed.append((f, dest, True))
    except FileExistsError:
        for f, dest, moved in reversed(placed):
            if moved:
                os.replace(dest, f)
            else:
                dest.unlink(missing_ok=True)
        raise
    for f, _dest, moved in placed:
        if not moved:
            f.unlink()


def _fsync(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def check_unchanged(signatures: dict[Path, tuple[int, int] | None]) -> None:
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
                specs.append(image_spec(info))
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
           "default_output", "unique_path", "make_plan", "execute", "open_source", "source_files", "reads_itself",
           "missing_qemu", "check_unchanged", "verify_images", "repair", "spec"]
