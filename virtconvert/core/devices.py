"""Physical disks: listing them, and reading one as the source of a virtual disk.

The disks come from lsblk, which needs no special rights. Reading a disk does
(root, or the "disk" group). VirtConvert&Create never runs qemu-img or itself as
root for that: a few lines of Python started with pkexec (the app) or sudo
(the command line) open the disk read-only and hand the open file back over a
Unix socket. The disk is then served to qemu-img through the NBD server
(nbd.py), the way a merged checkpoint chain is, so the copy runs as the user,
can be cancelled, and nothing is ever written to the disk.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import formats as F
from .errors import ConvertError, OperationCancelled
from .nbd import _socket_parent
from .partitions import GPT_TYPES, MBR_TYPES, Partition, PartitionTable, analyze
from .probe import ImageInfo
from .util import human_bytes

LSBLK_COLUMNS = ("NAME,KNAME,TYPE,SIZE,MODEL,VENDOR,SERIAL,TRAN,RM,RO,LOG-SEC,PHY-SEC,PTTYPE,PARTTYPE,"
                 "PARTFLAGS,FSTYPE,LABEL,MOUNTPOINT")
SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "/home", "[SWAP]")
# eMMC boot areas and replay-protected blocks are separate devices of the same chip
NOT_DISKS = re.compile(r"^(zram|ram|loop|nbd|mmcblk\d+(boot\d+|rpmb))")
KINDS = {"usb": "USB", "nvme": "NVMe", "sata": "SATA", "ata": "ATA", "sas": "SAS", "scsi": "SCSI",
         "mmc": "SD card", "virtio": "virtual", "iscsi": "iSCSI", "fc": "Fibre Channel"}

HELPER = r'''
import os, socket, stat, sys
path, where = sys.argv[1], sys.argv[2]
if not stat.S_ISBLK(os.stat(path).st_mode) and sys.argv[3:] != ["--any-file"]:
    sys.exit(path + " is not a disk")
fd = os.open(path, os.O_RDONLY)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(where)
socket.send_fds(s, [b"fd"], [fd])
s.close()
'''


@dataclass
class DiskPart:
    name: str
    path: str
    size: int
    fstype: str = ""
    label: str = ""
    parttype: str = ""       # GPT type GUID, or the MBR type as "0x83"
    bootable: bool = False   # MBR active flag


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
            table.partitions.append(Partition(n, 0, p.size, kind, p.label, bootable=p.bootable))
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


def _mounts(entry: dict) -> list[str]:
    found = []
    for key in ("mountpoint", "mountpoints"):
        value = entry.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found += [v for v in value if v]
    for child in entry.get("children", []) or []:
        found += _mounts(child)
    return list(dict.fromkeys(found))


def _disk(entry: dict) -> PhysicalDisk:
    name = _value(entry, "kname", "") or _value(entry, "name", "")
    disk = PhysicalDisk(
        path=f"/dev/{name}", name=name, size=_number(entry.get("size")), kind=_value(entry, "type", "disk"),
        model=str(_value(entry, "model", "")).strip(), vendor=str(_value(entry, "vendor", "")).strip(),
        serial=str(_value(entry, "serial", "")).strip(), transport=str(_value(entry, "tran", "")).strip(),
        removable=_flag(entry.get("rm")), read_only=_flag(entry.get("ro")),
        logical_sector=_number(entry.get("log-sec"), 512), physical_sector=_number(entry.get("phy-sec"), 512),
        table=str(_value(entry, "pttype", "")), mountpoints=_mounts(entry))
    # lsblk's VENDOR is often a placeholder ("ATA", "Generic", a PCI ID) or repeats the model
    if (disk.vendor.upper() in ("ATA", "GENERIC", "NVME", "") or disk.vendor.lower() in disk.model.lower()
            or re.fullmatch(r"0x[0-9a-fA-F]+", disk.vendor)):
        disk.vendor = ""
    for child in entry.get("children", []) or []:
        if _value(child, "type") == "part":
            cname = _value(child, "kname", "") or _value(child, "name", "")
            disk.partitions.append(DiskPart(
                name=cname, path=f"/dev/{cname}", size=_number(child.get("size")),
                fstype=str(_value(child, "fstype", "")), label=str(_value(child, "label", "")),
                parttype=str(_value(child, "parttype", "")),
                bootable=str(_value(child, "partflags", "")).lower() == "0x80"))
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
                timeout: float = 600, _helper_args: tuple[str, ...] = ()) -> int:
    """An open, read-only file descriptor of the disk PATH, asking for the administrator
    password if the user may not read it."""
    path = str(path)
    try:
        return os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except PermissionError as exc:
        if os.geteuid() == 0:     # root and still refused: a container, or a security module
            raise ConvertError(f"Cannot open {path}: {exc.strerror or exc}") from None
    except OSError as exc:
        raise ConvertError(f"Cannot open {path}: {exc.strerror or exc}") from None
    command = elevator(how)
    if command is None:
        raise ConvertError(f"Reading {path} needs administrator rights, and neither pkexec nor sudo can ask "
                           "for them here. Run VirtConvert&Create as root (sudo virtconvert …).")
    return _open_with_helper(path, command, cancel or (lambda: False), timeout, _helper_args)


def _open_with_helper(path: str, command: list[str], cancel: Callable[[], bool], timeout: float,
                      extra: tuple[str, ...]) -> int:
    folder = tempfile.mkdtemp(prefix="virtconvert-", dir=_socket_parent())
    where = os.path.join(folder, "fd")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    proc = None
    try:
        server.bind(where)
        server.listen(1)
        server.settimeout(0.2)
        proc = subprocess.Popen([*command, sys.executable, "-c", HELPER, path, where, *extra],
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
                    raise ConvertError(f"Reading {path} needs administrator rights, and they weren't given "
                                       "(the password dialog was cancelled or refused).")
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


__all__ = ["PhysicalDisk", "DiskPart", "list_disks", "disk_info", "is_device", "inspect_device", "info_from_disk",
           "disks_holding", "base_disks", "elevator", "open_device", "DeviceReader"]
