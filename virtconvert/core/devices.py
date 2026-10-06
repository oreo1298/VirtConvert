"""Physical disks: listing them, reading one as the source of a virtual disk, and
opening one to write a virtual disk onto it (writedisk.py).

The disks come from lsblk, which needs no special rights. Opening a disk does
(root, or the "disk" group). VirtConvert&Create never runs qemu-img or itself as
root for that: a few lines of Python started with pkexec (the app) or sudo
(the command line) open the disk and hand the open file back over a Unix
socket. The disk is then served to qemu-img through the NBD server (nbd.py),
the way a merged checkpoint chain is, so the copy runs as the user and can be
cancelled. A disk being copied is opened read-only and never written to; one
being written is opened exclusively (O_EXCL), which the kernel refuses while
anything on it is mounted or in use.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import formats as F
from .errors import ConvertError, OperationCancelled
from .nbd import _socket_parent
from .partitions import GPT_TYPES, MBR_TYPES, Partition, PartitionTable, analyze
from .probe import ImageInfo
from .util import MIB, human_bytes

LSBLK_COLUMNS = ("NAME,KNAME,TYPE,SIZE,MODEL,VENDOR,SERIAL,TRAN,RM,RO,LOG-SEC,PHY-SEC,PTTYPE,PARTTYPE,"
                 "PARTFLAGS,FSTYPE,LABEL,MOUNTPOINT")
SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "/home", "[SWAP]",
                 # the USB stick or DVD a live system runs from (Ubuntu, Debian, Fedora, Arch)
                 "/cdrom", "/run/live/medium", "/lib/live/mount/medium", "/run/initramfs/live",
                 "/run/archiso/bootmnt", "/run/archiso/img_dev")
# eMMC boot areas and replay-protected blocks are separate devices of the same chip
NOT_DISKS = re.compile(r"^(zram|ram|loop|nbd|mmcblk\d+(boot\d+|rpmb))")
KINDS = {"usb": "USB", "nvme": "NVMe", "sata": "SATA", "ata": "ATA", "sas": "SAS", "scsi": "SCSI",
         "mmc": "SD card", "virtio": "virtual", "iscsi": "iSCSI", "fc": "Fibre Channel"}

HELPER = r'''
import os, socket, stat, sys
path, where, mode = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    if not stat.S_ISBLK(os.stat(path).st_mode) and sys.argv[4:] != ["--any-file"]:
        sys.exit(path + " is not a disk")
    fd = os.open(path, os.O_RDWR | os.O_EXCL if mode == "w" else os.O_RDONLY)
except OSError as e:
    sys.exit("errno %d %s" % (e.errno, e.strerror))
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(where)
socket.send_fds(s, [b"fd"], [fd])
s.close()
'''
BLKZEROOUT = 0x127F          # _IO(0x12, 127): zero a range of the disk (the disk does it itself when it can)
ZERO_CHUNK = 64 * MIB
WRITEBACK_EVERY = 64 * MIB   # start writing the cache to the disk this often, so the progress is honest


@dataclass
class DiskPart:
    name: str
    path: str
    size: int
    fstype: str = ""
    label: str = ""
    parttype: str = ""       # GPT type GUID, or the MBR type as "0x83"
    bootable: bool = False   # MBR active flag
    start: int = -1          # bytes from the disk's start (from /sys; -1: unknown)


@dataclass
class PhysicalDisk:
    path: str
    name: str
    size: int
    kind: str = "disk"           # lsblk TYPE: disk, part, loop
    model: str = ""
    vendor: str = ""
    serial: str = ""
    transport: str = ""
    removable: bool = False
    read_only: bool = False
    logical_sector: int = 512
    physical_sector: int = 512
    table: str = ""              # gpt, dos
    partitions: list[DiskPart] = field(default_factory=list)
    mountpoints: list[str] = field(default_factory=list)   # of the disk and everything on it
    mounted: list[tuple[str, str]] = field(default_factory=list)   # (device, mountpoint): the disk's own
    holders: list[str] = field(default_factory=list)       # "luks-1 (crypt)": LVM, RAID, open encrypted volumes
    fstype: str = ""             # a file system on the whole disk (no partition table)
    label: str = ""
    seq: int = 0                 # the kernel's diskseq: new for every disk or medium plugged in (0: unknown)

    @property
    def title(self) -> str:
        """'Samsung SSD 870 EVO 500GB (sda)'."""
        maker = " ".join(dict.fromkeys(w for w in (self.vendor, self.model) if w))
        return f"{maker} ({self.name})" if maker else self.name

    @property
    def bus(self) -> str:
        return KINDS.get(self.transport, self.transport.upper()) if self.transport else ""

    @property
    def system(self) -> bool:
        return any(m in SYSTEM_MOUNTS for m in self.mountpoints)

    @property
    def summary(self) -> str:
        """'500 GB · SATA · GPT, 3 partitions · in use (/, /home)'."""
        parts = [human_bytes(self.size)]
        if self.bus:
            parts.append(("removable " if self.removable and self.bus != "USB" else "") + self.bus)
        if self.kind == "part":
            parts.append("a partition")
        elif self.table:
            count = len(self.partitions)
            parts.append(f"{'MBR' if self.table == 'dos' else self.table.upper()}, "
                         f"{count} partition{'s' if count != 1 else ''}")
        elif self.partitions == [] and self.kind == "disk":
            parts.append("no partition table")
        if self.mountpoints:
            shown = ", ".join(self.mountpoints[:3]) + ("…" if len(self.mountpoints) > 3 else "")
            parts.append(f"in use ({shown})")
        return " · ".join(parts)

    @property
    def stem(self) -> str:
        """A file name for its image: the model, or the device name."""
        text = self.model or self.vendor or self.name
        cleaned = re.sub(r"[^\w.+-]+", " ", text).strip()
        return cleaned[:60] or self.name

    def partition_table(self) -> PartitionTable | None:
        """What lsblk knows about the partitions, as the PartitionTable a disk image gets."""
        if self.kind == "part":
            return PartitionTable("none")
        if not self.table:
            return None
        scheme = "gpt" if self.table == "gpt" else "mbr" if self.table == "dos" else ""
        if not scheme:
            return None
        table = PartitionTable(scheme, sector_size=self.logical_sector)
        for n, p in enumerate(self.partitions, 1):
            if scheme == "gpt":
                kind = GPT_TYPES.get(p.parttype.lower(), "Other")
            else:
                try:
                    kind = MBR_TYPES.get(int(p.parttype, 16), f"Type {p.parttype}")
                except ValueError:
                    kind = "Other"
            table.partitions.append(Partition(n, max(p.start, 0), p.size, kind, p.label, bootable=p.bootable))
        table.positions_known = all(p.start >= 0 for p in self.partitions)
        return table


# ---------------------------------------------------------------------- listing

def _value(entry: dict, key: str, default=None):
    value = entry.get(key, default)
    return default if value is None else value


def _flag(value) -> bool:
    """lsblk writes booleans as true/false or, before 2.33, as "1"/"0"."""
    return value in (True, 1, "1", "true")


def _number(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _own_mounts(entry: dict) -> list[str]:
    found = []
    for key in ("mountpoint", "mountpoints"):
        value = entry.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found += [v for v in value if v]
    return list(dict.fromkeys(found))


def _mounts(entry: dict) -> list[str]:
    found = _own_mounts(entry)
    for child in entry.get("children", []) or []:
        found += _mounts(child)
    return list(dict.fromkeys(found))


def _holders(entry: dict) -> list[str]:
    """What sits on top of a disk or partition, other than partitions: LVM volumes, RAID arrays,
    opened encrypted volumes."""
    found = []
    for child in entry.get("children", []) or []:
        kind = str(_value(child, "type", ""))
        if kind != "part":
            found.append(f"{_value(child, 'name', '') or _value(child, 'kname', '')} ({kind})")
        found += _holders(child)
    return found


def _sys_value(name: str, key: str) -> str:
    """/sys/class/block/NAME/KEY (a partition's start in 512-byte units, a disk's diskseq), or ''."""
    try:
        with open(f"/sys/class/block/{name}/{key}", encoding="ascii") as f:
            return f.read().strip()
    except (OSError, ValueError):
        return ""


def backing_file(device: str | os.PathLike) -> str | None:
    """The file behind a loop device (writing onto it writes into that file), or None."""
    name = os.path.basename(os.path.realpath(device))
    return _sys_value(name, "loop/backing_file") or None


def _disk(entry: dict) -> PhysicalDisk:
    name = _value(entry, "kname", "") or _value(entry, "name", "")
    disk = PhysicalDisk(
        path=f"/dev/{name}", name=name, size=_number(entry.get("size")), kind=_value(entry, "type", "disk"),
        model=str(_value(entry, "model", "")).strip(), vendor=str(_value(entry, "vendor", "")).strip(),
        serial=str(_value(entry, "serial", "")).strip(), transport=str(_value(entry, "tran", "")).strip(),
        removable=_flag(entry.get("rm")), read_only=_flag(entry.get("ro")),
        logical_sector=_number(entry.get("log-sec"), 512), physical_sector=_number(entry.get("phy-sec"), 512),
        table=str(_value(entry, "pttype", "")), mountpoints=_mounts(entry), holders=_holders(entry),
        fstype=str(_value(entry, "fstype", "")), label=str(_value(entry, "label", "")),
        seq=_number(_sys_value(name, "diskseq")))
    disk.mounted = [(disk.path, m) for m in _own_mounts(entry)]
    # lsblk's VENDOR is often a placeholder ("ATA", "Generic", a PCI ID) or repeats the model
    if (disk.vendor.upper() in ("ATA", "GENERIC", "NVME", "") or disk.vendor.lower() in disk.model.lower()
            or re.fullmatch(r"0x[0-9a-fA-F]+", disk.vendor)):
        disk.vendor = ""
    for child in entry.get("children", []) or []:
        if _value(child, "type") == "part":
            cname = _value(child, "kname", "") or _value(child, "name", "")
            start = _number(_sys_value(cname, "start"), -1)       # always 512-byte units
            disk.partitions.append(DiskPart(
                name=cname, path=f"/dev/{cname}", size=_number(child.get("size")),
                fstype=str(_value(child, "fstype", "")), label=str(_value(child, "label", "")),
                parttype=str(_value(child, "parttype", "")),
                bootable=str(_value(child, "partflags", "")).lower() == "0x80",
                start=start * 512 if start >= 0 else -1))
            disk.mounted += [(f"/dev/{cname}", m) for m in _own_mounts(child)]
    return disk


def _lsblk(*paths: str) -> list[dict]:
    exe = shutil.which("lsblk") or shutil.which("lsblk", path="/usr/sbin:/sbin:/usr/bin:/bin")
    if exe is None:
        raise ConvertError("lsblk (from util-linux) is needed to find the disks.")
    try:
        res = subprocess.run([exe, "-J", "-b", "-o", LSBLK_COLUMNS, *paths], capture_output=True, text=True,
                             timeout=30, env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConvertError(f"lsblk failed: {exc}") from None
    if res.returncode != 0:
        detail = res.stderr.strip().splitlines()[-1] if res.stderr.strip() else f"exit code {res.returncode}"
        raise ConvertError(f"lsblk failed: {detail}")
    try:
        return json.loads(res.stdout).get("blockdevices", []) or []
    except ValueError:
        raise ConvertError("lsblk's output couldn't be read.") from None


def list_disks(include_loop: bool = False) -> list[PhysicalDisk]:
    """The computer's disks (hard disks, SSDs, USB sticks, memory cards), without empty card
    readers, RAM disks and, unless INCLUDE_LOOP, loop devices."""
    disks = []
    for entry in _lsblk():
        disk = _disk(entry)
        if disk.kind not in ("disk", "loop") or disk.size <= 0:
            continue
        if disk.kind == "loop" and not include_loop:
            continue
        if NOT_DISKS.match(disk.name) and not (include_loop and disk.name.startswith("loop")):
            continue
        disks.append(disk)
    return disks


def disk_info(path: str | os.PathLike) -> PhysicalDisk:
    """What lsblk says about the disk (or partition) PATH."""
    entries = _lsblk(str(path))
    if not entries:
        raise ConvertError(f"lsblk doesn't know {path}.")
    return _disk(entries[0])


def is_device(path: str | os.PathLike) -> bool:
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def inspect_device(path: str | os.PathLike) -> ImageInfo:
    """A physical disk (or partition) as an ImageInfo of a raw image. Nothing is read from the
    disk itself: everything comes from lsblk."""
    try:
        disk = disk_info(path)
    except ConvertError as exc:
        info = ImageInfo(path=Path(path), format=F.RAW, device=True)
        info.errors.append(str(exc))
        return info
    return info_from_disk(disk, Path(path))


def info_from_disk(disk: PhysicalDisk, path: Path | None = None) -> ImageInfo:
    """The ImageInfo of DISK (reading its partition table when that needs no password)."""
    path = path or Path(disk.path)
    info = ImageInfo(path=path, format=F.RAW, device=True)
    info.title = disk.title
    info.device_stem = disk.stem
    info.virtual_size = info.file_size = info.allocated = disk.size
    info.subformat = "physical " + ("partition" if disk.kind == "part" else "disk") + \
        (f", {disk.bus}" if disk.bus else "")
    info.creator = " ".join(w for w in (disk.vendor, disk.model) if w)
    info.disk_id = disk.serial
    info.logical_sector = disk.logical_sector
    info.physical_sector = disk.physical_sector
    info.partitions = disk.partition_table()
    info.mountpoints = disk.mountpoints
    if os.access(path, os.R_OK):
        # readable without a password (root, the disk group): the partition table itself is more
        # reliable than what lsblk has from udev
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                head = os.pread(fd, 128 * 1024, 0)
            finally:
                os.close(fd)
            if disk.kind != "part" and len(head) >= 1024:
                info.partitions = analyze(head, disk.logical_sector)
        except OSError:
            pass
    if disk.size <= 0:
        info.errors.append(f"{disk.name} has no medium (an empty card reader?).")
    if disk.kind == "part":
        info.notes.append("This is one partition: the image holds just its file system, without a partition "
                          "table. To start a computer from the copy, copy the whole disk instead.")
    return info


# ---------------------------------------------------------------------- where a folder lives

def _sys_disks(sysdir: str) -> set[str]:
    """The whole disks under a /sys/class/block entry (a partition's disk, an LVM or LUKS
    volume's disks)."""
    real = os.path.realpath(sysdir)
    if not os.path.isdir(real):
        return set()
    if os.path.exists(os.path.join(real, "partition")):
        return {os.path.basename(os.path.dirname(real))}
    slaves = os.path.join(real, "slaves")
    below = os.listdir(slaves) if os.path.isdir(slaves) else []
    if below:
        return set().union(*(_sys_disks(os.path.join(slaves, s)) for s in below))
    return {os.path.basename(real)}


def _mount_source(path: str) -> str:
    """The device mounted where PATH lives (for file systems like btrfs, whose st_dev isn't
    a real device)."""
    best, source = "", ""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                fields = line.split()
                if " - " not in line or len(fields) < 5:
                    continue
                mountpoint = fields[4].replace("\\040", " ")
                after = line.split(" - ", 1)[1].split()
                if (path == mountpoint or path.startswith(mountpoint.rstrip("/") + "/")) and len(mountpoint) >= len(best):
                    best, source = mountpoint, after[1] if len(after) > 1 else ""
    except OSError:
        pass
    return source


def disks_holding(path: str | os.PathLike) -> set[str]:
    """Names of the whole disks the folder PATH is stored on (empty when unknown)."""
    path = os.path.realpath(path)
    try:
        dev = os.stat(path).st_dev
    except OSError:
        return set()
    found = _sys_disks(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}") if os.major(dev) else set()
    if not found:
        source = _mount_source(path)
        if source.startswith("/dev/"):
            found = _sys_disks(f"/sys/class/block/{os.path.basename(os.path.realpath(source))}")
    return found


def base_disks(device: str | os.PathLike) -> set[str]:
    """The whole disks of the device node DEVICE (itself, or the disk a partition is on)."""
    return _sys_disks(f"/sys/class/block/{os.path.basename(os.path.realpath(device))}")


# ---------------------------------------------------------------------- reading

def elevator(how: str = "auto") -> list[str] | None:
    """The command that runs the helper as root: pkexec (a password dialog) or sudo (the
    terminal). HOW is 'pkexec', 'sudo' or 'auto'."""
    pkexec, sudo = shutil.which("pkexec"), shutil.which("sudo")
    graphical = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if how == "pkexec":
        return [pkexec] if pkexec else ([sudo, "--"] if sudo and sys.stdin.isatty() else None)
    if how == "sudo" or (how == "auto" and sys.stdin.isatty()):
        if sudo:
            return [sudo, "--"]
    if pkexec and (graphical or how == "sudo"):
        return [pkexec]
    return None


def open_device(path: str | os.PathLike, how: str = "auto", cancel: Callable[[], bool] | None = None,
                timeout: float = 600, _helper_args: tuple[str, ...] = (), write: bool = False) -> int:
    """An open file descriptor of the disk PATH, asking for the administrator password if the user
    may not open it: read-only, or with WRITE for reading and writing, exclusively (the kernel
    refuses that while something on the disk is mounted or in use, and refuses mounting it
    while the file is open)."""
    path = str(path)
    flags = (os.O_RDWR | os.O_EXCL if write else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags)
    except PermissionError as exc:
        if os.geteuid() == 0:     # root and still refused: write-protected, a container, a security module
            raise ConvertError(_open_error(path, exc.errno, exc.strerror or str(exc), write)) from None
    except OSError as exc:
        raise ConvertError(_open_error(path, exc.errno, exc.strerror or str(exc), write)) from None
    command = elevator(how)
    doing = "Writing to" if write else "Reading"
    if command is None:
        raise ConvertError(f"{doing} {path} needs administrator rights, and neither pkexec nor sudo can ask "
                           "for them here. Run VirtConvert&Create as root (sudo virtconvert …).")
    return _open_with_helper(path, command, cancel or (lambda: False), timeout, _helper_args, write)


def _open_error(path: str, number: int | None, text: str, write: bool) -> str:
    if write and number == errno.EBUSY:
        return (f"{path} is in use: it or a partition on it is mounted, or it belongs to LVM, a RAID array or an "
                "open encrypted volume. Unmount or close it, then try again.")
    if write and number == errno.EROFS:
        return f"{path} can't be written to: it is write-protected (a memory card's lock switch?)."
    if number == getattr(errno, "ENOMEDIUM", 123):
        return f"{path} has no medium (an empty card reader?)."
    if number in (errno.ENOENT, errno.ENXIO, errno.ENODEV):
        return f"{path} is gone (was it unplugged?)."
    return f"Cannot open {path}: {text}"


def _open_with_helper(path: str, command: list[str], cancel: Callable[[], bool], timeout: float,
                      extra: tuple[str, ...], write: bool = False) -> int:
    folder = tempfile.mkdtemp(prefix="virtconvert-", dir=_socket_parent())
    where = os.path.join(folder, "fd")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    proc = None
    doing = "Writing to" if write else "Reading"
    try:
        server.bind(where)
        server.listen(1)
        server.settimeout(0.2)
        proc = subprocess.Popen([*command, sys.executable, "-c", HELPER, path, where, "w" if write else "r", *extra],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + timeout
        while True:
            if cancel():
                raise OperationCancelled()
            try:
                conn, _ = server.accept()
                break
            except socket.timeout:
                pass
            if proc.poll() is not None:
                detail = (proc.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()
                if proc.returncode in (126, 127) and Path(command[0]).name == "pkexec":
                    raise ConvertError(f"{doing} {path} needs administrator rights, and they weren't given "
                                       "(the password dialog was cancelled or refused).")
                found = re.fullmatch(r"errno (\d+) (.*)", detail[-1]) if detail else None
                if found:
                    raise ConvertError(_open_error(path, int(found.group(1)), found.group(2), write))
                raise ConvertError(f"Couldn't open {path} as administrator: "
                                   f"{detail[-1] if detail else f'exit code {proc.returncode}'}")
            if time.monotonic() > deadline:
                raise ConvertError(f"Opening {path} as administrator took too long.")
        with conn:
            conn.settimeout(30)
            _msg, fds, _flags, _addr = socket.recv_fds(conn, 16, 1)
        if not fds:
            raise ConvertError(f"Couldn't open {path} as administrator.")
        fd = fds[0]
        os.set_inheritable(fd, False)
        # make sure it is the disk that was asked for
        if extra != ("--any-file",) and os.fstat(fd).st_rdev != os.stat(path).st_rdev:
            os.close(fd)
            raise ConvertError(f"Opening {path} gave a different device.")
        return fd
    finally:
        server.close()
        if proc is not None:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
            if proc.stderr:
                proc.stderr.close()
        shutil.rmtree(folder, ignore_errors=True)


def unmount(device: str, mountpoint: str, how: str = "auto") -> None:
    """Unmount DEVICE (mounted at MOUNTPOINT) the way the file manager does: with udisksctl, which
    needs no password for removable disks. Failing that, umount: as root, or through pkexec or
    sudo (HOW, see elevator)."""
    attempts = []
    if shutil.which("udisksctl"):
        attempts.append([shutil.which("udisksctl"), "unmount", "-b", device])
    umount = shutil.which("umount") or shutil.which("umount", path="/usr/sbin:/sbin:/usr/bin:/bin")
    if umount:
        command = [] if os.geteuid() == 0 else elevator(how)
        if command is not None:
            attempts.append([*command, umount, device])
    reason = "neither udisksctl nor umount could be used"
    for command in attempts:
        try:
            res = subprocess.run(command, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                                 env=dict(os.environ, LC_ALL="C"))
        except (OSError, subprocess.SubprocessError) as exc:
            reason = str(exc)
            continue
        if res.returncode == 0:
            return
        lines = (res.stderr or res.stdout).strip().splitlines()
        reason = lines[-1] if lines else f"exit code {res.returncode}"
    raise ConvertError(f"{device} (mounted at {mountpoint}) couldn't be unmounted: {reason}. Close the files that "
                       "are open on it, unmount it in the file manager, and try again.")


class DeviceReader:
    """Reads an open disk for the NBD server; zeros past its end (SIZE can be larger)."""

    def __init__(self, fd: int, size: int, name: str):
        self.fd = fd
        self.name = name
        self.device_size = os.lseek(fd, 0, os.SEEK_END)
        self.size = max(size, self.device_size)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        except (AttributeError, OSError):
            pass

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self.size - offset))
        end = min(offset + length, self.device_size)
        chunks = []
        pos = offset
        while pos < end:
            try:
                data = os.pread(self.fd, end - pos, pos)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    raise ConvertError(f"{self.name} can't be read at {pos:,} bytes (input/output error): a "
                                       "damaged sector? Copy the disk with ddrescue first, then convert that "
                                       "image.") from None
                raise ConvertError(f"Reading {self.name} failed at {pos:,} bytes: {exc.strerror or exc}") from None
            if not data:
                raise ConvertError(f"{self.name} ended at {pos:,} bytes, before its size "
                                   f"({self.device_size:,} bytes). Was it removed?")
            chunks.append(data)
            pos += len(data)
        data = b"".join(chunks)
        return data + bytes(length - len(data)) if len(data) < length else data

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class DeviceWriter:
    """Writes the first SIZE bytes of a disk opened for writing (writedisk.py) for the writable NBD
    server, and reads them back for the comparison afterwards. Requests run one at a time, and
    close() waits for the one in progress (an NBD thread may still be in one when the copy is
    cancelled)."""

    def __init__(self, fd: int, size: int, name: str):
        self.fd = fd
        self.name = name
        self.device_size = os.lseek(fd, 0, os.SEEK_END)
        if size > self.device_size:
            raise ConvertError(f"{name} holds {self.device_size:,} bytes, fewer than the {size:,} to write.")
        self.size = size
        self._zeroout = True
        self._unflushed = 0
        self._lock = threading.RLock()
        self._closing = False

    def _open(self) -> None:
        if self._closing or self.fd < 0:
            raise ConvertError(f"Writing to {self.name} was stopped.")

    def read(self, offset: int, length: int) -> bytes:
        with self._lock:
            self._open()
            return self._read(offset, length)

    def _read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self.size - offset))
        chunks = []
        pos, end = offset, offset + length
        while pos < end:
            try:
                data = os.pread(self.fd, end - pos, pos)
            except OSError as exc:
                raise self._failed("read", pos, exc) from None
            if not data:
                raise ConvertError(f"{self.name} ended at {pos:,} bytes. Was it removed?")
            chunks.append(data)
            pos += len(data)
        return b"".join(chunks)

    def write(self, offset: int, data: bytes) -> None:
        with self._lock:
            self._open()
            self._write(offset, data)

    def _write(self, offset: int, data: bytes) -> None:
        view = memoryview(data)
        done = 0
        while done < len(view):
            try:
                n = os.pwrite(self.fd, view[done:], offset + done)
            except OSError as exc:
                raise self._failed("written", offset + done, exc) from None
            if n <= 0:
                raise ConvertError(f"{self.name} ended at {offset + done:,} bytes. Was it removed?")
            done += n
        self._written(len(data))

    def zero(self, offset: int, length: int) -> None:
        """Zeros: whole sectors by the kernel (BLKZEROOUT, which lets the disk do it when it can),
        the rest, or everything on a disk without it, written."""
        with self._lock:
            self._open()
            self._zero(offset, length)

    def _zero(self, offset: int, length: int) -> None:
        pos, end = offset, offset + length
        first, last = -(-pos // 512) * 512, end // 512 * 512
        if self._zeroout and last > first:
            if first > pos:
                self._write_zeros(pos, first - pos)
            pos = first
            while pos < last and self._zeroout:
                self._open()            # stop between chunks when closing
                step = min(ZERO_CHUNK, last - pos)
                try:
                    fcntl.ioctl(self.fd, BLKZEROOUT, struct.pack("=QQ", pos, step))
                    pos += step
                except OSError as exc:
                    if exc.errno in (errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS):
                        self._zeroout = False       # not a disk (tests), or a very old kernel
                    else:
                        raise self._failed("written", pos, exc) from None
        self._write_zeros(pos, end - pos)

    def _write_zeros(self, offset: int, length: int) -> None:
        block = bytes(min(length, 4 * MIB))
        while length > 0:
            self._open()
            n = min(length, len(block))
            self._write(offset, block[:n])
            offset += n
            length -= n

    def _written(self, n: int) -> None:
        # start writing the cache to the disk now and then (POSIX_FADV_DONTNEED does that, and drops
        # what is written), so a slow USB stick doesn't get gigabytes "written" into the memory that
        # take minutes to reach it at the end
        self._unflushed += n
        if self._unflushed >= WRITEBACK_EVERY:
            self._unflushed = 0
            self.drop_cache()

    def flush(self) -> None:
        with self._lock:
            self._open()
            try:
                os.fsync(self.fd)
            except OSError as exc:
                raise self._failed("written", None, exc) from None

    def drop_cache(self) -> None:
        """Forget what's cached of the disk, so reading it again reads the disk itself."""
        if self.fd < 0:
            return
        try:
            os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass

    def _failed(self, what: str, pos: int | None, exc: OSError) -> ConvertError:
        where = f" at {pos:,} bytes" if pos is not None else ""
        if exc.errno == errno.EIO:
            return ConvertError(f"{self.name} can't be {what}{where} (input/output error): the disk may be failing, "
                                "or it was unplugged.")
        if exc.errno == errno.ENOSPC:
            return ConvertError(f"{self.name} ended{where}, before the end of what had to be written.")
        if what == "written" and exc.errno in (errno.EPERM, errno.EROFS, errno.EACCES):
            # Linux opens a write-protected disk for writing, and refuses the first write
            return ConvertError(f"{self.name} can't be written to: it is write-protected (a memory card's lock "
                                "switch?).")
        verb = "Writing to" if what == "written" else "Reading"
        return ConvertError(f"{verb} {self.name} failed{where}: {exc.strerror or exc}")

    def close(self) -> None:
        self._closing = True
        with self._lock:
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1


__all__ = ["PhysicalDisk", "DiskPart", "list_disks", "disk_info", "is_device", "inspect_device", "info_from_disk",
           "disks_holding", "base_disks", "elevator", "open_device", "unmount", "DeviceReader", "DeviceWriter"]
