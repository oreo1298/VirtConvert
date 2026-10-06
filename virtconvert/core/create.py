"""Creating new virtual disks: empty, with a partition table, or with a formatted partition.

A new disk is put together as a sparse raw file in a scratch folder: the
partition table is written into it, and the file system is made by the usual
mkfs program in a sparse file of the partition's size, whose data is then
copied in at the partition's offset (holes stay holes, so only the file
system's own structures take space). qemu-img writes the chosen format from
that, skipping the holes, and the new disk is compared and published like a
converted one (convert.write_image).
"""

from __future__ import annotations

import bisect
import errno
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import formats as F
from . import ova, system
from . import partitions as P
from .chain import ChainReader
from .convert import (
    Appliance,
    CancelFn,
    Outcome,
    ProgressFn,
    Target,
    check_output,
    check_space,
    work_folder,
    write_image,
)
from .errors import ConvertError, OperationCancelled, os_error_text
from .layers import DATA
from .probe import probe
from .qemu import QemuImg, source_spec
from .util import MIB, TIB, human_bytes, round_up

SCHEMES = ("none", "gpt", "mbr")
COPY_CHUNK = 4 * MIB


@dataclass(frozen=True)
class FileSystem:
    key: str
    label: str
    tools: tuple[str, ...]     # mkfs programs, the first one found is used
    package: str               # key of system.MKFS_PACKAGES
    windows: bool              # partition type: Microsoft basic data (else Linux file system)
    mbr_type: int
    min_size: int              # smallest partition mkfs accepts
    max_size: int | None
    max_label: int             # label length (characters; bytes for ext4 and XFS)
    title: str


FILESYSTEMS: tuple[FileSystem, ...] = (
    FileSystem("ntfs", "NTFS", ("mkntfs", "mkfs.ntfs"), "ntfs", True, 0x07, 8 * MIB, None, 32,
               "Windows; Linux reads and writes it, macOS reads it."),
    FileSystem("exfat", "exFAT", ("mkfs.exfat", "mkexfatfs"), "exfat", True, 0x07, 8 * MIB, None, 11,
               "Windows, macOS and Linux, for sharing files between them."),
    FileSystem("fat32", "FAT32", ("mkfs.fat", "mkfs.vfat", "mkdosfs"), "fat", True, 0x0C, 33 * MIB, 2 * TIB, 11,
               "Every system and firmware; files up to 4 GB."),
    FileSystem("ext4", "ext4", ("mkfs.ext4", "mke2fs"), "ext4", False, 0x83, 8 * MIB, None, 16,
               "Linux's standard file system."),
    FileSystem("btrfs", "Btrfs", ("mkfs.btrfs",), "btrfs", False, 0x83, 109 * MIB, None, 255,
               "Linux, with snapshots and compression."),
    FileSystem("xfs", "XFS", ("mkfs.xfs",), "xfs", False, 0x83, 300 * MIB, None, 12,
               "Linux, for large files and servers."),
)
FS_BY_KEY = {fs.key: fs for fs in FILESYSTEMS}
FAT_LABEL_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !#$%&'()-@^_`{}~")


# what a file system's label limit counts
LABEL_UNITS = {"ext4": "bytes", "xfs": "bytes", "btrfs": "bytes", "exfat": "utf16", "ntfs": "utf16"}


def cut(text: str, limit: int, unit: str = "chars") -> str:
    """TEXT shortened to LIMIT characters, UTF-8 bytes or UTF-16 code units, never in the middle
    of a character."""
    used = 0
    kept = []
    for c in text:
        size = len(c.encode("utf-8")) if unit == "bytes" else (2 if ord(c) > 0xFFFF else 1) if unit == "utf16" else 1
        if used + size > limit:
            break
        kept.append(c)
        used += size
    return "".join(kept)


def clean_label(fs: FileSystem, label: str) -> str:
    """LABEL as FS can store it: FAT labels are upper case without special characters, and
    every file system has a length limit (in characters, UTF-8 bytes or UTF-16 units)."""
    label = " ".join(label.split())
    if fs.key == "fat32":
        label = "".join(c for c in label.upper() if c in FAT_LABEL_CHARS)
    return cut(label, fs.max_label, LABEL_UNITS.get(fs.key, "chars")).strip()


def parse_size(text: str) -> int:
    """'20G', '512 MB', '1.5T', '2048' (megabytes) -> bytes. Binary units, like the rest of the app."""
    cleaned = text.strip().upper().replace(" ", "").removesuffix("IB").removesuffix("B")
    units = {"K": 1 << 10, "M": MIB, "G": 1 << 30, "T": TIB}
    factor = MIB
    if cleaned and cleaned[-1] in units:
        factor = units[cleaned[-1]]
        cleaned = cleaned[:-1]
    try:
        value = float(cleaned)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value * factor > 1 << 62:
        raise ConvertError(f"'{text}' is not a size; write it like 20G, 512M or 1.5T.")
    if value <= 0:
        raise ConvertError("The size must be larger than zero.")
    return int(value * factor)


@dataclass
class NewDisk:
    output: Path
    target: Target
    size: int
    scheme: str = "none"            # partition table: none, gpt or mbr
    filesystem: str = ""            # FileSystem key; "" leaves the partition unformatted
    label: str = ""
    verify: bool = True
    overwrite: bool = False


@dataclass
class CreatePlan:
    disk: NewDisk
    size: int = 0
    partition: tuple[int, int] | None = None     # offset, length
    label: str = ""
    tool: str | None = None
    needed: int = 0
    free: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def filesystem(self) -> FileSystem | None:
        return FS_BY_KEY.get(self.disk.filesystem)

    @property
    def summary(self) -> str:
        """'20 GB VDI (dynamic), GPT, NTFS “Data”'."""
        parts = [f"{human_bytes(self.size)} {self.disk.target.label}"]
        if self.disk.scheme != "none":
            parts.append(self.disk.scheme.upper())
            fs = self.filesystem
            parts.append(f"{fs.label}" + (f" “{self.label}”" if self.label else "") if fs else "unformatted")
        else:
            parts.append("no partition table" if self.disk.filesystem else "empty")
        return ", ".join(parts)


def make_create_plan(disk: NewDisk, qemu: QemuImg | None) -> CreatePlan:
    """Check everything that can be checked before creating the disk."""
    disk.target = target = disk.target.normalized()
    disk.scheme = disk.scheme.lower()
    if not disk.output.is_absolute():
        disk.output = Path(os.path.abspath(disk.output))
    fmt = target.format
    plan = CreatePlan(disk=disk)
    size = round_up(max(disk.size, 0), MIB)
    if disk.size <= 0:
        plan.errors.append("Enter a size for the disk.")
    elif size != disk.size:
        plan.notes.append(f"The size is rounded up to whole megabytes: {size:,} bytes.")
    plan.size = size
    if not fmt.writable:
        plan.errors.append(f"{fmt.label} can be read but not written.")
    if fmt.max_size and size > fmt.max_size:
        plan.errors.append(f"{fmt.label} files can't hold more than {fmt.max_label}; choose "
                           f"{'VHDX' if fmt is F.VHD else 'another format'}.")

    fs = None
    if disk.scheme not in SCHEMES:
        plan.errors.append(f"Unknown partition table '{disk.scheme}': use gpt, mbr or none.")
    if disk.filesystem:
        fs = FS_BY_KEY.get(disk.filesystem.lower())
        if fs is None:
            plan.errors.append(f"Unknown file system '{disk.filesystem}': use one of "
                               f"{', '.join(f.key for f in FILESYSTEMS)}.")
        else:
            disk.filesystem = fs.key
    if fs is not None and disk.scheme == "none":
        plan.errors.append("A file system goes into a partition: choose a GPT or MBR partition table.")
    if disk.scheme == "mbr" and size > 2 * TIB:
        plan.errors.append("An MBR partition table reaches 2 TB; choose GPT for larger disks.")
    if disk.scheme in ("gpt", "mbr") and size:
        offset, length = P.single_partition(size, disk.scheme)
        minimum = fs.min_size if fs else MIB
        if length < minimum:
            what = f"A {fs.label} partition" if fs else "A partition"
            # the partition starts at 1 MB; GPT keeps its backup in the last megabyte
            smallest = round_up(minimum, MIB) + (2 if disk.scheme == "gpt" else 1) * MIB
            plan.errors.append(f"{what} needs a disk of at least {human_bytes(smallest)}.")
        else:
            plan.partition = (offset, length)
            if fs is not None and fs.max_size and length > fs.max_size:
                plan.errors.append(f"{fs.label} partitions can't be larger than {human_bytes(fs.max_size)}; "
                                   "choose exFAT or NTFS.")
    if fs is not None:
        plan.tool = system.find_program(*fs.tools)
        if plan.tool is None:
            plan.errors.append(f"Making {fs.label} needs {fs.tools[0]}, which isn't installed. Install it "
                               f"with: {system.install_command(system.package_for(fs.package))}")
        plan.label = clean_label(fs, disk.label)
        if disk.label.strip() and plan.label != " ".join(disk.label.split()):
            plan.notes.append(f"{fs.label} labels are limited: the label will be “{plan.label}”.")

    if qemu is None:
        plan.errors.append("qemu-img is not installed. Run 'virtconvert doctor' to see how to install it.")
    elif qemu.too_old:
        plan.errors.append(f"qemu-img {qemu.version_text} is too old; version 2.10 or newer is needed.")
    else:
        for driver in qemu.missing_drivers(tuple(dict.fromkeys((fmt.driver, "raw")))):
            plan.errors.append(f"The qemu-img on this system was built without the '{driver}' format.")

    check_output(plan, disk.output, target, size, disk.overwrite, [])
    check_space(plan, disk.output, size if target.fixed else _metadata_estimate(fs, size), fmt)
    _notes(plan, fs)
    return plan


def _metadata_estimate(fs: FileSystem | None, size: int) -> int:
    """About how much a dynamic new disk takes while it's made (the file system's structures are
    there twice for a moment): FAT32 writes its tables in full, NTFS its bitmap and log."""
    if fs is None:
        return 16 * MIB
    extra = {"fat32": size // 300, "ntfs": 3 * (size // 32768 + 70 * MIB)}.get(fs.key, size // 2000)
    return 16 * MIB + extra


def _notes(plan: CreatePlan, fs: FileSystem | None) -> None:
    disk, target = plan.disk, plan.disk.target
    if disk.scheme == "none" and fs is not None:
        return                  # an error says what's wrong
    if disk.scheme == "none":
        plan.notes.append("The disk is empty: install an operating system on it, or partition and format "
                          "it inside the virtual machine.")
    elif fs is None:
        plan.notes.append("One unformatted partition fills the disk; format it inside the virtual machine.")
    else:
        plan.notes.append(f"One {fs.label} partition fills the disk: attach it to a virtual machine as an "
                          "extra disk and it's ready to use.")
        if not fs.windows:
            plan.notes.append(f"Windows can't read {fs.label} without extra software.")
        if fs.key == "fat32":
            plan.notes.append("FAT32 can't store files larger than 4 GB.")
    if target.stream:
        plan.notes.append("Stream-optimized VMDK files are for VMware ESXi and appliances; VirtualBox can't "
                          "use one directly.")
    if target.format is F.OVA:
        plan.notes.append("An appliance with this disk and a basic machine (2 CPUs, 2 GB, NAT network): "
                          "import it with File → Import Appliance.")
    if target.multi_file:
        files = target.output_files(disk.output, plan.size)
        plan.notes.append(f"The VMDK consists of {len(files)} files ({files[0].name} and "
                          f"{files[1].name}{'…' if len(files) > 2 else ''}); keep them together.")


# ---------------------------------------------------------------------- creating

def mkfs_command(fs: FileSystem, tool: str, path: Path, label: str, offset: int) -> list[str]:
    """The command that makes FS in the file PATH, a partition starting OFFSET bytes into the disk."""
    hidden = str(offset // 512)
    if fs.key == "fat32":
        cmd = [tool, "-F", "32", "-S", "512", "-h", hidden] + (["-n", label] if label else [])
    elif fs.key == "exfat":
        if _exfatprogs(tool):
            cmd = [tool] + (["-L", label] if label else [])
        else:                               # the older exfat-utils (mkexfatfs)
            cmd = [tool, "-p", hidden] + (["-n", label] if label else [])
    elif fs.key == "ntfs":
        # quick format, a file rather than a device, the partition's place for Windows' boot code
        cmd = [tool, "-Q", "-F", "-q", "-s", "512", "-p", hidden, "-H", "255", "-S", "63"]
        cmd += ["-L", label] if label else []
    elif fs.key == "ext4":
        # the file is all zeros, so the inode tables and the journal needn't be written; the
        # bitmaps and inode tables go together at the start instead of every 2 GB, which keeps
        # a dynamic disk small
        cmd = [tool, "-t", "ext4", "-F", "-q", "-E", "lazy_itable_init=1,lazy_journal_init=1,packed_meta_blocks=1"]
        cmd += ["-L", label] if label else []
    elif fs.key in ("btrfs", "xfs"):
        cmd = [tool, "-f", "-q"] + (["-L", label] if label else [])
    else:
        raise ConvertError(f"Unknown file system {fs.key}")
    return cmd + [str(path)]


def _exfatprogs(tool: str) -> bool:
    """exfatprogs' mkfs.exfat (-L LABEL) rather than the older exfat-utils' mkexfatfs (-n LABEL)."""
    if Path(tool).name == "mkexfatfs":
        return False
    try:
        res = subprocess.run([tool, "-V"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True
    return "mkexfatfs" not in res.stdout + res.stderr


def _run(cmd: list[str], cancel: CancelFn) -> None:
    # English messages, but a UTF-8 character set: mkfs.exfat converts labels with it
    ctype = os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE") or os.environ.get("LANG") or ""
    env = {k: v for k, v in os.environ.items() if k not in ("LC_ALL", "LANGUAGE")}
    env["LC_MESSAGES"] = "C"
    env["LC_CTYPE"] = ctype if re.search(r"utf-?8", ctype, re.I) else "C.UTF-8"
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, start_new_session=True)
    except OSError as exc:
        raise ConvertError(f"Could not run {Path(cmd[0]).name}: {exc.strerror or exc}") from None
    try:
        while proc.poll() is None:
            if cancel():
                proc.kill()
                proc.wait()
                raise OperationCancelled()
            time.sleep(0.05)
    finally:
        output = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        if proc.stdout:
            proc.stdout.close()
    if proc.returncode != 0:
        lines = [line for line in output.strip().splitlines() if line.strip()]
        detail = "; ".join(lines[-3:]) or f"exit code {proc.returncode}"
        raise ConvertError(f"{Path(cmd[0]).name} failed: {detail}")


def copy_allocated(src: Path, dst_fd: int, offset: int, cancel: CancelFn) -> int:
    """Copy what SRC stores into DST_FD at OFFSET, leaving its holes (and runs of zeros) out.
    Returns the number of bytes written."""
    seek_data, seek_hole = getattr(os, "SEEK_DATA", None), getattr(os, "SEEK_HOLE", None)
    written = 0
    with open(src, "rb") as f:
        fd = f.fileno()
        size = os.fstat(fd).st_size
        pos = 0
        while pos < size:
            start, end = pos, size
            if seek_data is not None:
                try:
                    start = os.lseek(fd, pos, seek_data)
                    end = os.lseek(fd, start, seek_hole)
                except OSError as exc:
                    if exc.errno == errno.ENXIO:
                        break               # only a hole is left
                    if exc.errno not in (errno.EINVAL, errno.EOPNOTSUPP):
                        raise
                    seek_data = None         # not supported here: read everything
                    start, end = pos, size
            while start < end:
                if cancel():
                    raise OperationCancelled()
                data = os.pread(fd, min(COPY_CHUNK, end - start), start)
                if not data:
                    raise ConvertError(f"{src.name} ended early while copying")
                if data.count(0) != len(data):
                    view = memoryview(data)
                    done = 0
                    while done < len(data):
                        done += os.pwrite(dst_fd, view[done:], offset + start + done)
                    written += len(data)
                start += len(data)
            pos = end
    return written


def data_ranges(fd: int, size: int) -> list[tuple[int, int]]:
    """(offset, length) of the parts of the open file FD that aren't holes."""
    seek_data, seek_hole = getattr(os, "SEEK_DATA", None), getattr(os, "SEEK_HOLE", None)
    if seek_data is None:
        return [(0, size)]
    ranges = []
    pos = 0
    try:
        while pos < size:
            try:
                start = os.lseek(fd, pos, seek_data)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                raise
            end = min(os.lseek(fd, start, seek_hole), size)
            ranges.append((start, end - start))
            pos = end
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.EOPNOTSUPP):
            return [(0, size)]
        raise
    return ranges


def _merged(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for start, length in sorted(ranges):
        if out and start <= out[-1][0] + out[-1][1]:
            out[-1][1] = max(out[-1][1], start + length - out[-1][0])
        else:
            out.append([start, length])
    return [(a, b) for a, b in out]


def stored_ranges(layer) -> list[tuple[int, int]]:
    """(offset, length) of the parts of the virtual disk the image file LAYER holds as data:
    its allocated blocks, minus the holes in the file (a fixed VHDX is mostly holes)."""
    file_data = data_ranges(layer.fd, layer.file_size)
    starts = [a for a, _ in file_data]
    found: list[tuple[int, int]] = []
    pos, step = 0, 64 * MIB
    while pos < layer.size:
        n = min(step, layer.size - pos)
        at = pos
        for kind, length, file_offset in layer.map(pos, n):
            if kind == DATA:
                i = max(0, bisect.bisect_right(starts, file_offset) - 1)
                while i < len(file_data) and file_data[i][0] < file_offset + length:
                    lo = max(file_data[i][0], file_offset)
                    hi = min(file_data[i][0] + file_data[i][1], file_offset + length)
                    if lo < hi:
                        found.append((at + lo - file_offset, hi - lo))
                    i += 1
            at += length
        pos += n
    return found


def compare_with_reader(raw: Path, size: int):
    """A comparison of a new VHD, VHDX or VDI with the raw disk it was written from, using
    VirtConvert&Create's own readers: only the parts either of them stores are read."""
    def compare(written: Path, percent: Callable[[float], None], cancel: CancelFn) -> tuple[bool, str]:
        info = probe(written)
        if not info.ok:
            return False, "; ".join(info.errors)
        if info.virtual_size != size:
            return False, f"the disk is {info.virtual_size:,} bytes instead of {size:,}"
        with ChainReader.for_image(info) as reader, open(raw, "rb") as f:
            fd = f.fileno()
            ranges = _merged(data_ranges(fd, size) + stored_ranges(reader.layers[0]))
            total = sum(length for _, length in ranges) or 1
            done = 0
            for start, length in ranges:
                pos, end = start, min(start + length, size)
                while pos < end:
                    if cancel():
                        raise OperationCancelled()
                    n = min(COPY_CHUNK, end - pos)
                    ours, theirs = os.pread(fd, n, pos), reader.read(pos, n)
                    if ours != theirs:
                        at = next((i for i in range(min(len(ours), len(theirs))) if ours[i] != theirs[i]), 0)
                        return False, f"Content mismatch at offset {pos + at:,}"
                    pos += n
                    done += n
                    percent(100.0 * done / total)
        return True, "Images are identical."
    return compare


def _sparse_ok(folder: Path) -> bool:
    """Whether FOLDER's file system keeps unwritten parts of a file as holes (FAT and exFAT
    write them out)."""
    test = folder / "sparse-test"
    try:
        with open(test, "wb") as f:
            f.truncate(64 * MIB)
        return os.stat(test).st_blocks * 512 < MIB
    except OSError:
        return False
    finally:
        test.unlink(missing_ok=True)


def table_for(plan: CreatePlan) -> dict[int, bytes]:
    """The partition table sectors of the new disk, as {offset: bytes}."""
    if plan.partition is None:
        return {}
    offset, length = plan.partition
    fs = plan.filesystem
    if plan.disk.scheme == "gpt":
        kind = P.BASIC_DATA if fs is None or fs.windows else P.LINUX_DATA
        return P.gpt_table(plan.size, [(offset, length, kind, plan.label)])
    return P.mbr_table([(offset, length, fs.mbr_type if fs else 0x07)])


def create_disk(plan: CreatePlan, qemu: QemuImg, progress: ProgressFn | None = None,
                cancel: CancelFn | None = None, log: Callable[[str], None] | None = None) -> Outcome:
    if not plan.ok:
        raise ConvertError(plan.errors[0])
    disk, size, fs = plan.disk, plan.size, plan.filesystem
    progress = progress or (lambda *_: None)
    cancel = cancel or (lambda: False)
    log = log or (lambda _m: None)
    out = disk.output
    name = ova.appliance_name(out.name[:-len(out.suffix)] if out.suffix else out.name)
    work = work_folder(out)
    work.mkdir()
    if plan.partition is None:
        # an empty disk: qemu-img makes it directly
        log(f"qemu-img: new empty disk → {disk.target.label}, {human_bytes(size)}")
        return write_image(qemu, None, size, disk.target, out, work, verify=disk.verify,
                           overwrite=disk.overwrite, appliance=Appliance(name), stage="Writing",
                           progress=progress, cancel=cancel, log=log)
    elsewhere: Path | None = None
    try:
        try:
            scratch = work / "scratch"
            scratch.mkdir()
            if not _sparse_ok(scratch):
                # a drive without sparse files (FAT, exFAT): build the disk in the temporary folder
                other = Path(tempfile.mkdtemp(prefix="virtconvert-"))
                if _sparse_ok(other):
                    scratch = elsewhere = other
                else:
                    shutil.rmtree(other, ignore_errors=True)
            raw = scratch / "disk.raw"
            with open(raw, "wb") as f:
                _truncate(f, size)
                for at, data in table_for(plan).items():
                    os.pwrite(f.fileno(), data, at)
                if fs is not None and plan.tool is not None:
                    offset, length = plan.partition
                    progress("Formatting", 0, 0)
                    log(f"{Path(plan.tool).name}: {fs.label} in a {human_bytes(length)} partition")
                    part = scratch / "partition.raw"
                    with open(part, "wb") as p:
                        _truncate(p, length)
                    _run(mkfs_command(fs, plan.tool, part, plan.label, offset), cancel)
                    if os.path.getsize(part) != length:
                        raise ConvertError(f"{Path(plan.tool).name} changed the size of the partition")
                    copy_allocated(part, f.fileno(), offset, cancel)
                    part.unlink()
                    progress("Formatting", 1, 1)
                os.fsync(f.fileno())
        except OSError as exc:
            shutil.rmtree(work, ignore_errors=True)
            raise ConvertError(f"Making the new disk failed: {os_error_text(exc)}") from None
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        log(f"qemu-img: new disk → {disk.target.label}, {human_bytes(size)}")
        # qemu-img can't skip the empty parts of a VHDX, so it would read all of a large new disk
        dynamic_vhdx = disk.target.format is F.VHDX and not disk.target.fixed
        own = compare_with_reader(raw, size) if disk.target.format is F.VHDX else None
        # Linux file systems keep metadata all over the disk: small VHDX blocks keep the file small
        # (Microsoft recommends 1 MB blocks for Linux guests)
        extra = {"block_size": str(MIB)} if dynamic_vhdx and fs is not None and not fs.windows else None
        return write_image(qemu, source_spec(raw, "raw"), size, disk.target, out, work, verify=disk.verify,
                           overwrite=disk.overwrite, appliance=Appliance(name), stage="Writing",
                           progress=progress, cancel=cancel, log=log, compare=own, extra_options=extra)
    finally:
        if elsewhere is not None:
            shutil.rmtree(elsewhere, ignore_errors=True)


def _truncate(f, size: int) -> None:
    try:
        f.truncate(size)
    except OSError as exc:
        if exc.errno == errno.EFBIG:
            raise ConvertError(f"A partitioned disk of {human_bytes(size)} is put together in a file that "
                               "large, and this drive's file system doesn't allow one (ext4 stops at 16 TB). "
                               "Create a smaller disk, or an empty one and partition it in the VM.") from None
        raise


def available_filesystems() -> dict[str, str | None]:
    """{file system key: its mkfs program, or None when it isn't installed}."""
    return {fs.key: system.find_program(*fs.tools) for fs in FILESYSTEMS}


__all__ = ["FILESYSTEMS", "FS_BY_KEY", "SCHEMES", "FileSystem", "NewDisk", "CreatePlan", "make_create_plan",
           "create_disk", "clean_label", "parse_size", "mkfs_command", "copy_allocated",
           "available_filesystems"]
