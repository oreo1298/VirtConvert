"""Writing a virtual disk onto a physical one: a USB stick, a memory card, an SSD or a hard disk.

Any disk VirtConvert&Create reads can be written: every format, checkpoints merged
with their parents, a disk inside an OVA, or another physical disk. The disk is
opened by the root helper (devices.open_device with write=True: exclusively, so the
kernel refuses while anything on it is mounted, and nothing can mount it meanwhile),
and qemu-img writes the image's contents into it through the writable NBD server,
so neither qemu-img nor this program runs as root. Everything is written, zeros
included, so nothing of the disk's old contents shows through.

Then the disk is read back and compared with the image, and fitted to the disk's
size: a larger disk gets its last megabyte cleared (old GPT, RAID and ZFS
signatures live there) and a GPT's backup moved to its end; a smaller disk is
accepted when the partitions fit, and gets the part of the image they are in.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import devices
from .convert import (
    CancelFn,
    ProgressFn,
    check_unchanged,
    guess_os,
    missing_qemu,
    open_source,
    reads_itself,
    source_files,
)
from .errors import ConvertError, OperationCancelled, VerifyError
from .images import HEAD_BYTES, inspect_image
from .nbd import NbdServer
from .partitions import GPT_ENTRIES, PartitionTable, analyze, gpt_room, move_backup_gpt
from .probe import ImageInfo
from .qemu import QemuImg, limited_spec, nbd_spec
from .util import MIB, file_signature, human_bytes, round_up, same_file

TAIL = MIB      # cleared at the end of a disk larger than the image


@dataclass
class DiskWrite:
    """Write SOURCE (an image, a disk in an OVA, or a physical disk) onto the physical disk DISK."""
    source: Path
    disk: Path
    verify: bool = True
    member: str | None = None
    parents: dict[Path, Path] = field(default_factory=dict)
    force_parent: bool = False
    candidates: list[Path] = field(default_factory=list)
    elevate: str = "auto"                  # how to open the disks: pkexec, sudo, auto (devices.elevator)
    expect: tuple | None = None            # disk_identity() of the disk the person chose
    partitions: tuple[int, ...] = ()       # only these partitions of the physical disk SOURCE


@dataclass
class WritePlan:
    job: DiskWrite
    info: ImageInfo
    disk: devices.PhysicalDisk | None = None
    size: int = 0                  # bytes written: the virtual disk, or the part the partitions are in
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    can_verify: bool = True        # false when the source is a physical disk in use
    unmount: list[tuple[str, str]] = field(default_factory=list)     # (device, mountpoint) to unmount first

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def uses_reader(self) -> bool:
        return reads_itself(self.info, self.info.virtual_size)

    @property
    def summary(self) -> str:
        """'Windows.vhdx (64 GB VHDX) → SanDisk Extreme (sdb), 128 GB'."""
        info, disk = self.info, self.disk
        source = f"{info.name} ({human_bytes(info.virtual_size)} {info.label})" if info.format else info.name
        return f"{source} → {disk.title}, {human_bytes(disk.size)}" if disk else source

    @property
    def erased(self) -> str:
        """What is on the disk now, for asking before erasing it: 'sdb1 “STICK” (vfat, 14.6 GB)'."""
        disk = self.disk
        if disk is None:
            return ""
        parts = []
        for p in disk.partitions:
            label = f" “{p.label}”" if p.label else ""
            what = ", ".join(x for x in (p.fstype, human_bytes(p.size)) if x)
            parts.append(f"{p.name}{label} ({what})")
        if not parts and disk.fstype:
            label = f" “{disk.label}”" if disk.label else ""
            return f"a {disk.fstype} file system{label} on the whole disk"
        return ", ".join(parts) if parts else "no partitions"


@dataclass
class WriteOutcome:
    disk: Path
    title: str
    size: int                  # bytes written
    seconds: float
    verified: bool
    fitted: bool = False       # the GPT was moved to the disk's end


def disk_identity(disk: devices.PhysicalDisk) -> tuple[int, str, str, int]:
    """What tells the disk the person chose from one plugged in under the same name meanwhile: the
    kernel numbers every disk and medium it sees (diskseq; two cards in one reader have the reader's
    model and serial number)."""
    return disk.size, disk.model, disk.serial, disk.seq


def partitions_end(table: PartitionTable | None) -> int | None:
    """Where the last partition ends (with the room a GPT's backup needs after it), or None without
    any, or when only their sizes are known."""
    if table is None or table.scheme not in ("gpt", "mbr") or not table.partitions or not table.positions_known:
        return None
    end = max(p.start + p.size for p in table.partitions)
    if table.scheme == "gpt":
        end += gpt_room(table.sector_size, table.entries_bytes or GPT_ENTRIES * 128)
    return end


def make_write_plan(job: DiskWrite, qemu: QemuImg | None, info: ImageInfo | None = None,
                    disk: devices.PhysicalDisk | None = None) -> WritePlan:
    """Check everything that can be checked before erasing the disk."""
    if info is None:
        info = inspect_image(job.source, qemu, job.parents, job.force_parent, job.candidates, job.member,
                             job.partitions)
    plan = WritePlan(job=job, info=info)
    plan.errors.extend(info.errors)
    plan.warnings.extend(info.warnings)
    plan.notes.extend(n for n in info.notes if not n.startswith("Comment:"))
    if info.format is None:
        return plan

    # the disk
    if not devices.is_device(job.disk):
        plan.errors.append(f"{job.disk} is not a disk. 'virtconvert disks' lists them.")
        return plan
    try:
        disk = disk or devices.disk_info(job.disk)
    except ConvertError as exc:
        plan.errors.append(str(exc))
        return plan
    plan.disk = disk
    if disk.kind == "part":
        whole = ", ".join(sorted(devices.base_disks(job.disk))) or "the disk it is on"
        plan.errors.append(f"{disk.name} is a partition. Choose the whole disk ({whole}): the virtual disk brings "
                           "its own partitions.")
        return plan
    if disk.kind not in ("disk", "loop"):
        plan.errors.append(f"{disk.name} is not a disk but a {disk.kind} device. 'virtconvert disks' lists the "
                           "disks.")
        return plan
    if disk.name.startswith("nbd"):
        plan.errors.append(f"{disk.name} is a network block device: what it writes to can't be checked (it could "
                           "be the image itself).")
        return plan
    if disk.size <= 0:
        plan.errors.append(f"{disk.name} has no medium (an empty card reader?).")
        return plan
    if job.expect is not None and disk_identity(disk) != job.expect:
        plan.errors.append(f"{disk.path} is not the disk that was chosen any more (was it unplugged, and another "
                           "one plugged in?). Choose it again.")
    if disk.read_only:
        plan.errors.append(f"{disk.title} is write-protected (a memory card's lock switch?).")
    shown = ", ".join(disk.mountpoints[:4]) + ("…" if len(disk.mountpoints) > 4 else "")
    if {m for m in disk.mountpoints if m in devices.SYSTEM_MOUNTS} == {"[SWAP]"}:
        plan.errors.append(f"{disk.title} is used as swap space: turn that off first (swapoff, or your "
                           "distribution's swap settings).")
    elif disk.system:
        plan.errors.append(f"{disk.title} holds the running system (in use at {shown}): it can't be "
                           "overwritten. To put a virtual disk on it, start the computer from a live USB "
                           "system and write it from there.")
    elif disk.holders:
        plan.errors.append(f"{disk.title} is in use by {', '.join(disk.holders)}: close the encrypted volume, "
                           "LVM volume group or RAID array on it first.")
    elif disk.mounted:
        plan.unmount = list(disk.mounted)
        plan.warnings.append(f"{', '.join(m for _d, m in disk.mounted)} on this disk "
                             f"{'is' if len(disk.mounted) == 1 else 'are'} unmounted first.")

    # never the disk the image is read from
    target = devices.base_disks(job.disk)
    if info.device:
        if devices.base_disks(info.path) & target:
            plan.errors.append("The source and the target are the same disk.")
    else:
        files = source_files(info)
        backing = devices.backing_file(job.disk)
        if backing and any(same_file(backing, f) for f in files):
            plan.errors.append(f"{disk.name} is a loop device of {Path(backing).name}, the image itself.")
        elif any(devices.disks_holding(f) & target for f in files):
            plan.errors.append(f"{info.name} is stored on {disk.title}, so it would be overwritten while it is "
                               "read. Copy it to another drive first.")

    # sizes and sectors
    virtual = info.virtual_size
    table = info.partitions
    if virtual <= disk.size:
        plan.size = round_up(virtual, 512)       # qemu-img writes whole sectors (zeros after an odd end)
        if disk.size - virtual >= MIB:
            plan.notes.append(f"The disk is {human_bytes(disk.size - virtual)} larger than the virtual disk; that "
                              "part stays unused. Enlarge the last partition (GParted, Windows' Disk "
                              "Management) to use it.")
    else:
        end = partitions_end(table)
        if end is not None and end <= disk.size:
            plan.size = disk.size
            plan.notes.append(f"The virtual disk ({human_bytes(virtual)}) is larger than this disk, but its "
                              f"partitions end at {human_bytes(end)}: the first {human_bytes(disk.size)} are "
                              "written, the rest is unused space.")
        else:
            reach = f" whose partitions end at {human_bytes(end)}" if end is not None else ""
            plan.errors.append(f"{disk.title} is too small: {human_bytes(disk.size)} for a {human_bytes(virtual)} "
                               f"virtual disk{reach}. Shrink the partitions inside the virtual machine so they "
                               f"end before {human_bytes(disk.size)}, or choose a larger disk.")
    if table is not None and table.scheme in ("gpt", "mbr") and table.sector_size != disk.logical_sector:
        plan.errors.append(f"{disk.title} has {disk.logical_sector}-byte sectors and the virtual disk's partition "
                           f"table is made for {table.sector_size}-byte ones: a computer wouldn't find the "
                           "partitions on it.")
    plan.errors += missing_qemu(qemu, ["raw", "nbd"] + ([] if plan.uses_reader else [info.format.driver]))

    # the source: a physical disk in use changes while it's read
    if info.device and info.mountpoints:
        plan.can_verify = False
        plan.warnings.append(f"{info.name} is in use (mounted at {', '.join(info.mountpoints[:3])}): its files "
                             "change while it is copied, and the copy can't be compared with it afterwards.")
    if not os.access(job.disk, os.W_OK) or (info.device and not os.access(info.path, os.R_OK)):
        plan.notes.append("Writing to a disk needs administrator rights: you'll be asked for your password.")
    firmware = table.firmware if table is not None else ""
    if firmware == "uefi":
        plan.notes.append("It starts with UEFI: set the computer's firmware to UEFI boot (not legacy/CSM).")
    elif firmware == "bios":
        plan.notes.append("It starts with BIOS (legacy) boot: on a UEFI computer, turn on CSM or legacy boot.")
    if guess_os(info) == "windows":
        plan.notes.append("Windows on other hardware than it was installed on may need drivers for the disk "
                          "controller, and may ask to be activated again.")
    return plan


def write_to_disk(plan: WritePlan, qemu: QemuImg, progress: ProgressFn | None = None,
                  cancel: CancelFn | None = None, log: Callable[[str], None] | None = None) -> WriteOutcome:
    """Erase the disk and write the image onto it, compare, and fit the partition table to the
    disk. Cancelling raises OperationCancelled with .written: false while the disk is only partly
    written, true when only the comparing (and fitting) was left out."""
    if not plan.ok or plan.disk is None:
        raise ConvertError(plan.errors[0] if plan.errors else "Nothing to write to.")
    disk = plan.disk
    progress = progress or (lambda *_: None)
    cancel = cancel or (lambda: False)
    log = log or (lambda _m: None)
    started = time.monotonic()
    state = {"written": False}
    try:
        verified, fitted = _write(plan, qemu, progress, cancel, log, state)
    except OperationCancelled as exc:
        exc.written = state["written"]
        raise
    return WriteOutcome(disk=Path(disk.path), title=disk.title, size=plan.size,
                        seconds=time.monotonic() - started, verified=verified, fitted=fitted)


def _write(plan: WritePlan, qemu: QemuImg, progress: ProgressFn, cancel: CancelFn, log: Callable[[str], None],
           state: dict) -> tuple[bool, bool]:
    job, info, disk, size = plan.job, plan.info, plan.disk, plan.size
    signatures = {p: file_signature(p) for p in source_files(info)}
    for device, mountpoint in plan.unmount:
        log(f"Unmounting {mountpoint} ({device})")
        devices.unmount(device, mountpoint, job.elevate)
    if cancel():
        raise OperationCancelled()
    with open_source(info, info.virtual_size, plan.uses_reader, job.elevate, cancel, log) as (source, source_error):
        if size < info.virtual_size:
            _check_partitions_fit(qemu, source, plan)
        image = limited_spec(source, size) if size < info.virtual_size else source
        log(f"Opening {disk.path} for writing")
        fd = devices.open_device(disk.path, job.elevate, cancel, write=True)
        try:
            writer = devices.DeviceWriter(fd, size, disk.title)
        except BaseException:
            os.close(fd)
            raise
        server = None
        try:
            # the password dialogs are over and the disk is held: is it still the one that was chosen?
            now = devices.disk_info(disk.path)
            if disk_identity(now) != disk_identity(disk) or writer.device_size != disk.size:
                raise ConvertError(f"{disk.path} is not the disk that was chosen any more (was it unplugged, and "
                                   "another one plugged in?). Nothing was written.")
            server = NbdServer(writer, writable=True).start()

            def failed(exc: Exception | None = None) -> None:
                message = source_error() or (str(server.error) if server.error is not None else None)
                if message:
                    raise ConvertError(message) from exc

            def stage(name: str):
                return lambda pct: progress(name, int(size * min(pct, 100.0) / 100), size)

            log(f"qemu-img: {info.label} → {disk.title}, {human_bytes(size)}")
            progress("Writing", 0, size)
            try:
                qemu.convert_into(image, server.url, progress=stage("Writing"), cancel=cancel)
            except ConvertError as exc:
                failed(exc)
                raise
            progress("Writing", size, size)
            log("Writing what is still in the computer's memory to the disk")
            progress("Finishing", 0, 0)
            writer.flush()
            state["written"] = True
            check_unchanged(signatures)
            verified = False
            if job.verify and plan.can_verify:
                if cancel():
                    raise OperationCancelled()
                log("Reading the disk back and comparing it with the image")
                writer.drop_cache()      # read the disk, not what the computer still remembers of it
                progress("Verifying", 0, size)
                try:
                    same, detail = qemu.compare(image, nbd_spec(server.socket_path, server.export),
                                                progress=stage("Verifying"), cancel=cancel)
                except ConvertError as exc:
                    failed(exc)
                    raise
                failed()
                if not same:
                    raise VerifyError(f"{disk.title} doesn't hold what was written: {detail} The disk may be "
                                      "failing (or a fake: a USB stick that claims more space than it has).")
                verified = True
                progress("Verifying", size, size)
            elif job.verify:
                log("Not comparing the disk with its source: the source is in use and keeps changing")
            server.stop()
            server = None
            fitted = _fit_to_disk(writer, plan, log)
            writer.flush()
        finally:
            if server is not None:
                server.stop()
            writer.close()
    return verified, fitted


def _check_partitions_fit(qemu: QemuImg, source: str, plan: WritePlan) -> None:
    """Before writing part of a larger image: read its partition table itself (the plan may only
    have had lsblk's sizes, or a guess at the sector size) and make sure the partitions end on the
    disk."""
    try:
        head = qemu.read_head(source, HEAD_BYTES)
    except ConvertError as exc:
        raise ConvertError(f"The partition table of {plan.info.name} couldn't be read: {exc}") from None
    logical = plan.info.logical_sector if plan.info.logical_sector != 512 else None
    end = partitions_end(analyze(head, logical))
    if end is None or end > plan.disk.size:
        reach = f": its partitions end at {human_bytes(end)}" if end is not None else ""
        raise ConvertError(f"{plan.info.name} doesn't fit on {plan.disk.title}{reach}. Nothing was written. Shrink "
                           "the partitions inside the virtual machine first.")


def _fit_to_disk(writer: devices.DeviceWriter, plan: WritePlan, log: Callable[[str], None]) -> bool:
    """After the image: clear the old end of a larger disk, and move a GPT's backup to the end."""
    disk_size = writer.device_size
    writer.size = disk_size                 # the whole disk from now on
    if disk_size > plan.size:
        start = max(plan.size, disk_size - TAIL)
        writer.zero(start, disk_size - start)
    table = plan.info.partitions
    if table is None or table.scheme != "gpt" or disk_size == plan.info.virtual_size:
        return False
    moved = move_backup_gpt(writer.read, writer.write, disk_size, table.sector_size)
    if moved:
        log("Moved the backup partition table to the end of the disk")
    elif plan.size < plan.info.virtual_size:
        # its backup and last usable sector lie past the end: Linux wouldn't find the partitions
        raise ConvertError(f"The partition table couldn't be fitted to {plan.disk.title}, which is smaller than the "
                           "virtual disk: computers won't find its partitions. Shrink the partitions inside the "
                           "virtual machine, then write it again.")
    else:
        log("The partition table couldn't be fitted to the disk's size; GParted or 'sgdisk -e' can do it")
    return moved


__all__ = ["DiskWrite", "WritePlan", "WriteOutcome", "make_write_plan", "write_to_disk", "disk_identity",
           "partitions_end"]
