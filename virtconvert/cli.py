"""Command line interface.

Without a command the app starts (with any image files given queued);
everything the app does is also available as a sub-command for scripting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .core import devices, system
from .core import formats as F
from .core.convert import (
    PRESETS,
    Job,
    default_output,
    default_target,
    execute,
    make_plan,
    parse_target,
    repair,
    verify_images,
)
from .core.create import FILESYSTEMS, NewDisk, available_filesystems, create_disk, make_create_plan, parse_size
from .core.errors import ConvertError, OperationCancelled, QemuImgMissingError, os_error_text
from .core.images import expand_sources, inspect_image
from .core.probe import ImageInfo
from .core.qemu import QemuImg
from .core.util import human_bytes, mmss
from .core.writedisk import DiskWrite, disk_identity, make_write_plan, write_to_disk

SPINNER = "|/-\\"
COMMANDS = ("gui", "info", "convert", "create", "write", "disks", "verify", "repair", "formats", "doctor")


class Progress:
    """A single-line progress display on stderr."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet or not sys.stderr.isatty()
        self.stage = None
        self.t0 = time.monotonic()
        self.last = 0.0

    def __call__(self, stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        if stage != self.stage:
            self.finish()
            self.stage, self.t0 = stage, now
        if self.quiet or (now - self.last < 0.1 and done < total):
            return
        self.last = now
        elapsed = now - self.t0
        if total:
            pct = 100.0 * done / total
            width = 30
            fill = int(width * done / total)
            rate = done / elapsed if elapsed > 0.2 else 0
            eta = (total - done) / rate if rate > 0 else 0
            line = (f"\r{stage:<11} [{'#' * fill}{'.' * (width - fill)}] {pct:5.1f}%  "
                    f"{human_bytes(rate) + '/s':>10}  ETA {mmss(eta)}")
        else:
            spin = SPINNER[int(elapsed * 4) % 4]
            line = f"\r{stage:<11} {spin}  {elapsed:5.1f}s"
        sys.stderr.write(line)
        sys.stderr.flush()

    def finish(self) -> None:
        if self.stage and not self.quiet:
            sys.stderr.write(f"\r{self.stage:<11} done in {mmss(time.monotonic() - self.t0)}"
                             + " " * 50 + "\n")
        self.stage = None


def _qemu(required: bool = True) -> QemuImg | None:
    q = QemuImg.find()
    if q is None and required:
        raise QemuImgMissingError(
            "qemu-img was not found. It does the actual conversion; install it with:\n"
            f"  {system.qemu_install_command()}")
    return q


def _print_messages(errors: list[str], warnings: list[str], notes: list[str], indent: str = "  ") -> None:
    for text in errors:
        print(f"{indent}✕ {text}")
    for text in warnings:
        print(f"{indent}! {text}")
    for text in notes:
        print(f"{indent}· {text}")


def _info_json(info: ImageInfo) -> dict:
    data = {
        "path": str(info.path), "member": info.member, "format": info.format.key if info.format else None,
        "subformat": info.subformat, "virtual_size": info.virtual_size, "file_size": info.file_size,
        "allocated": info.allocated, "block_size": info.block_size,
        "logical_sector_size": info.logical_sector, "disk_id": info.disk_id, "creator": info.creator,
        "differencing": info.differencing, "parents": [str(p.path) for p in info.chain],
        "backing_file": info.backing_file, "dirty": info.dirty,
        "partitions": None, "errors": info.errors, "warnings": info.warnings, "notes": info.notes,
    }
    if info.partitions is not None:
        data["partitions"] = {"scheme": info.partitions.scheme, "firmware": info.partitions.firmware,
                              "entries": [asdict(p) for p in info.partitions.partitions]}
    return data


# ---------------------------------------------------------------------- commands

def cmd_info(args) -> int:
    qemu = _qemu(required=False)
    infos = [inspect_image(path, qemu, candidates=args.parent, member=member)
             for f in args.files for path, member in expand_sources(f)]
    if args.json:
        print(json.dumps([_info_json(i) for i in infos] if len(infos) > 1 else _info_json(infos[0]),
                         indent=2))
        return 0 if all(i.ok for i in infos) else 1
    for n, info in enumerate(infos):
        if n:
            print()
        print(f"{info.path} › {info.member}" if info.member else info.path)
        if info.format is not None and info.virtual_size:
            width = 12
            for key, value in info.describe():
                print(f"  {key:<{width}} {value}")
            if info.partitions is not None and info.partitions.partitions:
                print(f"  {'':<{width}} {info.partitions.summary()}")
            if info.ok:
                target = default_target(info)
                print(f"  {'Suggested':<{width}} convert to {target.format.label} "
                      f"({', '.join(target.format.works_with)})")
        _print_messages(info.errors, info.warnings, info.notes)
    return 0 if all(i.ok for i in infos) else 1


def cmd_convert(args) -> int:
    qemu = _qemu()
    sources = [s for f in args.files for s in expand_sources(f)]
    if args.output and len(sources) > 1:
        raise SystemExit("--output works with one disk; use --dir for several."
                         + (" (The appliance contains several disks.)" if len(args.files) == 1 else ""))
    if args.dir and not Path(args.dir).is_dir():
        raise SystemExit(f"{args.dir} is not a folder.")
    candidates = [Path(p) for p in args.parent]
    failures = 0
    planned: set[Path] = set()
    for n, (source, member) in enumerate(sources):
        if n:
            print()
        parents = {}
        if args.force_parent and len(candidates) == 1:
            parents = {source: candidates[0]}
        info = inspect_image(source, qemu, parents, args.force_parent, candidates, member)
        target = parse_target(args.to or default_target(info).format.key, fixed=args.fixed, split=args.split)
        if args.fixed and not target.fixed:
            print(f"  ! {target.label} has no preallocated variant; making a dynamic one.")
        if args.split and not target.split:
            print(f"  ! Only VMDK files can be split; making one {target.format.label} file.")
        if args.output:
            output = Path(args.output)
        else:
            output = default_output(info, target, Path(args.dir) if args.dir else None,
                                    unique=False)
            if output in planned:       # two disks of one appliance with the same name
                output = default_output(info, target, output.parent, taken=planned)
        planned.add(output)
        job = Job(source=source, target=target, output=output, verify=not args.no_verify,
                  overwrite=args.overwrite, parents=parents, force_parent=args.force_parent,
                  candidates=candidates, member=member)
        plan = make_plan(job, qemu, info)
        print(f"{info.name} → {job.output}")
        print(f"  {info.label}, {human_bytes(info.virtual_size)}  →  {job.target.label}"
              + (f", {human_bytes(plan.size)}" if plan.size != info.virtual_size else ""))
        _print_messages(plan.errors, plan.warnings, [] if args.quiet else plan.notes)
        if not plan.ok:
            failures += 1
            continue
        if plan.warnings and not args.yes and sys.stdin.isatty():
            if input("  Continue anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                failures += 1
                continue
        progress = Progress(args.quiet)
        try:
            outcome = execute(plan, qemu, progress)
        except OperationCancelled:
            raise
        except ConvertError as exc:
            progress.finish()
            print(f"  ✕ {exc}")
            failures += 1
            continue
        finally:
            progress.finish()
        check = ", verified identical to the source" if outcome.verified else ""
        files = f"{len(outcome.files)} files" if len(outcome.files) > 1 else "file"
        print(f"  ✓ {outcome.output.name}: {human_bytes(outcome.file_size)} {files}{check} "
              f"({mmss(outcome.seconds)})")
    return 1 if failures else 0


def cmd_create(args) -> int:
    qemu = _qemu()
    output = Path(args.output)
    if args.to:
        target = parse_target(args.to, fixed=args.fixed, split=args.split)
        if not output.suffix:
            output = output.with_name(output.name + target.format.extension)
    else:
        fmt = F.by_extension(output.name)
        if fmt is None or not fmt.writable:
            raise SystemExit(f"Say which format {output.name} should be with --to (vdi, vmdk, vhd, vhdx, "
                             "qcow2, qed, ova, raw), or give it that extension.")
        target = parse_target(fmt.key, fixed=args.fixed, split=args.split)
    if args.fixed and not target.fixed:
        print(f"! {target.label} has no preallocated variant; making a dynamic one.")
    if args.split and not target.split:
        print(f"! Only VMDK files can be split; making one {target.format.label} file.")
    if args.source:
        if args.size or args.fs or args.table or args.label:
            raise SystemExit("--from copies a disk as it is: leave out --size, --fs, --table and --label.")
        return _copy_disk(args, qemu, Path(args.source), target, output)
    if not args.size:
        raise SystemExit("Give the size of the new disk with --size (e.g. -s 64G), or copy a disk with --from.")
    if args.label and not args.fs:
        print("! --label names a file system, and without --fs there is none: it's left out.")
    scheme = args.table or ("gpt" if args.fs else "none")
    disk = NewDisk(output=output, target=target, size=parse_size(args.size), scheme=scheme,
                   filesystem=args.fs or "", label=args.label or "", verify=not args.no_verify,
                   overwrite=args.overwrite)
    plan = make_create_plan(disk, qemu)
    print(f"New disk {disk.output}")
    print(f"  {plan.summary}")
    _print_messages(plan.errors, plan.warnings, [] if args.quiet else plan.notes)
    if not plan.ok:
        return 1
    if plan.warnings and not args.yes and sys.stdin.isatty():
        if input("  Continue anyway? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    progress = Progress(args.quiet)
    try:
        outcome = create_disk(plan, qemu, progress)
    finally:
        progress.finish()
    check = ", checked" if outcome.verified else ""
    files = f"{len(outcome.files)} files" if len(outcome.files) > 1 else "file"
    print(f"  ✓ {outcome.output.name}: {human_bytes(outcome.file_size)} {files}{check} ({mmss(outcome.seconds)})")
    return 0


def _copy_disk(args, qemu: QemuImg, source: Path, target, output: Path) -> int:
    """create --from: a virtual disk with the contents of a physical one."""
    if not devices.is_device(source):
        raise SystemExit(f"{source} is not a disk. 'virtconvert disks' lists them; to convert an image "
                         "file use 'virtconvert convert'.")
    info = inspect_image(source, qemu)
    job = Job(source=source, target=target, output=output, verify=not args.no_verify,
              overwrite=args.overwrite)
    plan = make_plan(job, qemu, info)
    print(f"New disk {job.output} from {info.name} ({source})")
    print(f"  {human_bytes(plan.size)} {job.target.label}, a copy of {info.subformat.split(', ')[0]}"
          f" {info.name}")
    _print_messages(plan.errors, plan.warnings, [] if args.quiet else plan.notes)
    if not plan.ok:
        return 1
    if plan.warnings and not args.yes and sys.stdin.isatty():
        if input("  Continue anyway? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    progress = Progress(args.quiet)
    try:
        outcome = execute(plan, qemu, progress)
    finally:
        progress.finish()
    check = ", verified identical to the disk" if outcome.verified else ""
    print(f"  ✓ {outcome.output.name}: {human_bytes(outcome.file_size)} file{check} ({mmss(outcome.seconds)})")
    return 0


def cmd_disks(args) -> int:
    disks = devices.list_disks(include_loop=args.all)
    if args.json:
        print(json.dumps([dict(asdict(d), title=d.title, bus=d.bus) for d in disks], indent=2))
        return 0
    if not disks:
        print("No disks found.")
        return 0
    width = max(len(d.path) for d in disks)
    for d in disks:
        print(f"{d.path:<{width}}  {d.title}")
        print(f"{'':<{width}}  {d.summary}")
        for part in d.partitions:
            what = " ".join(x for x in (part.fstype, f"“{part.label}”" if part.label else "") if x)
            print(f"{'':<{width}}    {part.name}: {human_bytes(part.size)}{'  ' + what if what else ''}")
    print("\nCopy one into a virtual disk:  virtconvert create disk.vdi --from /dev/sdX")
    print("Write a virtual disk onto one:  virtconvert write disk.vdi /dev/sdX   (erases it)")
    return 0


def cmd_write(args) -> int:
    qemu = _qemu()
    sources = expand_sources(args.image)
    if args.member:
        sources = [s for s in sources if s[1] == args.member] or [(Path(args.image), args.member)]
    elif len(sources) > 1:
        raise SystemExit(f"{args.image} holds {len(sources)} disks ({', '.join(m for _p, m in sources)}); "
                         "choose one with --member.")
    source, member = sources[0]
    candidates = [Path(p) for p in args.parent]
    parents = {source: candidates[0]} if args.force_parent and len(candidates) == 1 else {}
    job = DiskWrite(source=source, disk=Path(args.disk), verify=not args.no_verify, member=member,
                    parents=parents, force_parent=args.force_parent, candidates=candidates)
    plan = make_write_plan(job, qemu)
    print(plan.summary)
    _print_messages(plan.errors, plan.warnings, [] if args.quiet else plan.notes)
    if not plan.ok:
        return 1
    disk = plan.disk
    question = (f"Everything on {disk.title} ({disk.path}, {human_bytes(disk.size)}; now: {plan.erased}) is "
                "erased.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit(f"{question} Add --yes to write without asking.")
        print(f"  {question}")
        if input(f"  Type the disk's name ({disk.name}) to erase it and write: ").strip() not in (disk.name, disk.path):
            print("  Nothing was written.")
            return 1
        # look again: the disk may have been swapped (or mounted) while the question was open
        job.expect = disk_identity(disk)
        plan = make_write_plan(job, qemu, plan.info)
        if not plan.ok:
            _print_messages(plan.errors, [], [])
            return 1
    progress = Progress(args.quiet)
    try:
        outcome = write_to_disk(plan, qemu, progress)
    except (OperationCancelled, KeyboardInterrupt) as exc:
        progress.finish()
        if getattr(exc, "written", False):
            print(f"\nStopped. {disk.path} is written, but wasn't compared with the image (or fitted to the disk's "
                  "size): write it again to be sure.", file=sys.stderr)
        else:
            print(f"\nStopped. {disk.path} is only partly written: write it again, or format it.", file=sys.stderr)
        return 130
    finally:
        progress.finish()
    check = ", read back and verified" if outcome.verified else ""
    print(f"  ✓ {human_bytes(outcome.size)} written to {outcome.title}{check} ({mmss(outcome.seconds)})")
    if outcome.fitted:
        print("  · The partition table was fitted to the disk's size.")
    return 0


def cmd_verify(args) -> int:
    qemu = _qemu()
    a = inspect_image(Path(args.first), qemu, candidates=args.parent)
    b = inspect_image(Path(args.second), qemu, candidates=args.parent)
    for info in (a, b):
        if not info.ok:
            _print_messages(info.errors, [], [], indent="")
            return 2
    progress = Progress(args.quiet)
    try:
        same, detail = verify_images(a, b, qemu, progress)
    finally:
        progress.finish()
    print(f"{'✓' if same else '✕'} {detail}")
    if a.virtual_size != b.virtual_size:
        print(f"  (sizes differ: {a.virtual_size:,} and {b.virtual_size:,} bytes; the extra part is "
              f"{'all zeros' if same else 'compared as zeros'})")
    return 0 if same else 1


def cmd_repair(args) -> int:
    qemu = _qemu()
    info = inspect_image(Path(args.file), qemu)
    if info.format is not F.VHDX or not info.dirty:
        print(f"{info.name} doesn't need repairing." if info.format is not None else
              "\n".join(info.errors))
        return 0 if info.format is not None else 1
    print(f"{info.name} has unapplied changes in its log (it wasn't closed cleanly).")
    print("Repairing applies them to the file, as Hyper-V would when it opens it.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("Add --yes to repair without asking.")
        if input("Change the file now? Make a copy first if you can. [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    print(repair(info, qemu))
    after = inspect_image(info.path, qemu)
    if after.dirty:
        print("✕ The log is still there; the repair did not work.")
        return 1
    print("✓ Repaired. You can convert it now.")
    return 0


def cmd_formats(args) -> int:
    qemu = _qemu(required=False)

    def show(fmt: F.Format) -> None:
        flag = ""
        if qemu is not None and qemu.formats and fmt.driver not in qemu.formats:
            flag = "  (not supported by this qemu-img)"
        print(f"  {fmt.key:<10} {', '.join(fmt.extensions):<22} {fmt.title}{flag}")

    print("Read and write:")
    for fmt in F.WRITABLE:
        show(fmt)
    print("\nRead only (convert them to one of the above):")
    for fmt in F.ALL:
        if not fmt.writable:
            show(fmt)
    print("\nTargets for --to (formats, or these shortcuts):")
    shortcuts = {"virtualbox": "VDI, dynamic", "hyperv": "VHDX, dynamic", "vmware": "VMDK, dynamic",
                 "esxi": "VMDK, stream-optimized (also: stream)", "azure": "VHD, fixed, whole MB",
                 "proxmox": "QCOW2 (also: kvm, qemu)"}
    for name, text in shortcuts.items():
        assert name in PRESETS or name in F.ALIASES
        print(f"  {name:<11} {text}")
    print("\nVariants: --fixed allocates the whole disk now (VDI, VHD, VHDX, QCOW2, raw; a VMDK gets a "
          "separate -flat data file),\n          --split makes a VMDK in 2 GB files.")
    print("\nWithout --to: VirtualBox disks (VDI, VMDK) become VHDX for Hyper-V; everything else "
          "becomes VDI for VirtualBox.")
    print("\nFile systems for new disks (virtconvert create --fs):")
    for fs, tool in zip(FILESYSTEMS, available_filesystems().values()):
        missing = "" if tool else f"  (needs {fs.tools[0]}: {system.install_command(system.package_for(fs.package))})"
        print(f"  {fs.key:<7} {fs.title}{missing}")
    return 0


def cmd_doctor(args) -> int:
    print(f"VirtConvert&Create {__version__} on {system.distro_name()}")
    problems = 0
    for check in system.doctor():
        mark = "✓" if check.ok else ("·" if check.optional else "✕")
        print(f"  {mark} {check.name:<18} {check.detail}")
        if not check.ok and check.hint:
            print(f"    {check.hint}")
        if not check.ok and not check.optional:
            problems += 1
    if args.install_qemu:
        if QemuImg.find() is not None:
            print("qemu-img is already installed.")
        else:
            ok, message = system.install_qemu()
            print(message)
            return 0 if ok else 1
    elif QemuImg.find() is None:
        print(f"\nInstall qemu-img with:  {system.qemu_install_command()}")
        print("or run:  virtconvert doctor --install-qemu")
    return 1 if problems else 0


# ---------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="virtconvert",
        description="Convert and create virtual disks for VirtualBox, Hyper-V, VMware, QEMU/KVM and "
                    "more (VDI, VMDK, VHD, VHDX, QCOW2, QED, OVA, raw; also reads Parallels, DMG, ISO). "
                    "Without a command the app starts.",
        epilog="Examples:\n"
               "  virtconvert                          open the app\n"
               "  virtconvert info disk.vhdx           show what an image contains\n"
               "  virtconvert convert disk.vhdx        -> disk.vdi for VirtualBox\n"
               "  virtconvert convert disk.vdi         -> disk.vhdx for Hyper-V\n"
               "  virtconvert convert -t vmdk *.vhdx   several at once\n"
               "  virtconvert convert -t azure os.vdi  fixed VHD for Azure\n"
               "  virtconvert convert -t ova vm.vhdx   an appliance for File > Import Appliance\n"
               "  virtconvert convert app.ova          each disk of an appliance -> VHDX\n"
               "  virtconvert verify disk.vhdx disk.vdi\n"
               "  virtconvert create new.vdi -s 50G   an empty 50 GB disk\n"
               "  virtconvert create data.vhdx -s 1T --fs ntfs --label Data\n"
               "  virtconvert disks                    list the physical disks\n"
               "  virtconvert create pc.vdi --from /dev/sda   a copy of a physical disk\n"
               "  virtconvert write vm.vhdx /dev/sdb   put a virtual disk onto a physical one\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"virtconvert {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("gui", help="open the app (the default)")
    p.add_argument("files", nargs="*", help="images to add to the list")

    p = sub.add_parser("info", help="show what an image contains")
    p.add_argument("files", nargs="+", metavar="IMAGE")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--parent", action="append", default=[], metavar="FILE",
                   help="where to look for the parent of a checkpoint/snapshot")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("convert", help="convert images",
                       description="Convert images. Without --to, VirtualBox disks (VDI, VMDK) become "
                                   "VHDX and everything else becomes VDI. An OVA's disks are "
                                   "converted one by one.")
    p.add_argument("files", nargs="+", metavar="IMAGE")
    p.add_argument("-t", "--to", metavar="FORMAT",
                   help="vdi, vmdk, vhd, vhdx, qcow2, qed, ova, raw, or virtualbox, hyperv, vmware, "
                        "esxi, azure, proxmox (see 'virtconvert formats')")
    p.add_argument("-o", "--output", metavar="FILE", help="the converted file (one source only)")
    p.add_argument("-d", "--dir", metavar="FOLDER", help="put converted files in this folder")
    p.add_argument("--fixed", action="store_true",
                   help="allocate the whole disk now (VDI, VHD, VHDX, QCOW2, raw, VMDK)")
    p.add_argument("--split", action="store_true", help="VMDK in 2 GB files (VMware Workstation)")
    p.add_argument("--no-verify", action="store_true",
                   help="don't compare the copy with the source afterwards")
    p.add_argument("--overwrite", action="store_true", help="replace existing files")
    p.add_argument("--parent", action="append", default=[], metavar="FILE",
                   help="the parent of a checkpoint/snapshot, if it isn't found by itself")
    p.add_argument("--force-parent", action="store_true",
                   help="use the --parent file even if its ID doesn't match (risky)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask about warnings")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress bar and notes")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("create", help="create a new disk: empty, formatted, or a copy of a physical disk",
                       description="Create a new virtual disk. The format comes from the file name's "
                                   "extension or --to. With --fs the disk gets one partition (GPT unless "
                                   "--table mbr) formatted with that file system, ready to use as an extra "
                                   "disk. With --from it becomes a copy of a physical disk (see "
                                   "'virtconvert disks'); you're asked for your password to read it.")
    p.add_argument("output", metavar="FILE", help="the new disk, e.g. data.vdi")
    p.add_argument("--from", dest="source", metavar="DISK",
                   help="copy this physical disk, e.g. /dev/sdb (the whole disk, or one partition)")
    p.add_argument("-s", "--size", help="its size: 20G, 512M, 2T (binary units)")
    p.add_argument("-t", "--to", metavar="FORMAT", help="vdi, vmdk, vhd, vhdx, qcow2, qed, ova, raw, or a "
                                                         "shortcut like hyperv or azure (default: from FILE)")
    p.add_argument("--fixed", action="store_true", help="allocate the whole disk now")
    p.add_argument("--split", action="store_true", help="VMDK in 2 GB files")
    p.add_argument("--table", choices=("gpt", "mbr", "none"),
                   help="partition table (default: gpt with --fs, none without)")
    p.add_argument("--fs", choices=[fs.key for fs in FILESYSTEMS], metavar="FS",
                   help=f"format the partition: {', '.join(fs.key for fs in FILESYSTEMS)} "
                        "(see 'virtconvert formats')")
    p.add_argument("-L", "--label", help="the file system's name")
    p.add_argument("--no-verify", action="store_true", help="don't check the new disk afterwards")
    p.add_argument("--overwrite", action="store_true", help="replace an existing file")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask about warnings")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress and notes")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("write", help="write a virtual disk onto a physical disk (erases it)",
                       description="Write a virtual disk onto a physical disk (a USB stick, a memory card, an "
                                   "SSD, a hard disk), so a computer can start from it. Any format works, "
                                   "checkpoints included. Everything on the disk is erased; you're asked to "
                                   "confirm, and for your password to open the disk. Afterwards the disk is "
                                   "read back and compared with the image. A larger disk keeps the rest "
                                   "unused; a smaller one works when the partitions fit.")
    p.add_argument("image", metavar="IMAGE", help="the virtual disk (or an OVA, or another physical disk)")
    p.add_argument("disk", metavar="DISK", help="the physical disk to overwrite, e.g. /dev/sdb "
                                                "(see 'virtconvert disks')")
    p.add_argument("--member", metavar="NAME", help="the disk of an OVA with several")
    p.add_argument("--no-verify", action="store_true", help="don't read the disk back and compare it")
    p.add_argument("--parent", action="append", default=[], metavar="FILE",
                   help="the parent of a checkpoint/snapshot, if it isn't found by itself")
    p.add_argument("--force-parent", action="store_true",
                   help="use the --parent file even if its ID doesn't match (risky)")
    p.add_argument("-y", "--yes", action="store_true", help="erase the disk without asking")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress and notes")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("disks", help="list the physical disks (to copy one, or to write onto one)")
    p.add_argument("--all", action="store_true", help="include loop devices")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_disks)

    p = sub.add_parser("verify", help="check that two images contain the same disk")
    p.add_argument("first", metavar="IMAGE")
    p.add_argument("second", metavar="IMAGE")
    p.add_argument("--parent", action="append", default=[], metavar="FILE")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("repair", help="apply the log of a VHDX that wasn't closed cleanly")
    p.add_argument("file", metavar="IMAGE")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("formats", help="list the formats and shortcuts")
    p.set_defaults(func=cmd_formats)

    p = sub.add_parser("doctor", help="check that everything VirtConvert&Create needs is installed")
    p.add_argument("--install-qemu", action="store_true",
                   help="install qemu-img with your package manager (asks for your password)")
    p.set_defaults(func=cmd_doctor)
    return ap


def _split_files(argv: list[str]) -> tuple[list[str], list[str]]:
    """`virtconvert disk.vhdx` opens the app with the file; tell files from commands."""
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        files = [a for a in argv if not a.startswith("-")]
        if all(os.path.exists(f) for f in files):
            return [a for a in argv if a.startswith("-")], files
    return argv, []


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, files = _split_files(argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command in (None, "gui"):
        files = getattr(args, "files", None) or files
        try:
            from .gui.app import run_gui
        except ImportError as exc:
            print(f"The app needs PySide6 (Qt 6), which isn't available: {exc}\n"
                  "Install it (see the README), or use the commands: virtconvert --help",
                  file=sys.stderr)
            return 1
        return run_gui(files)
    try:
        return args.func(args)
    except OperationCancelled:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing was left behind (the unfinished copy was removed).", file=sys.stderr)
        return 130
    except ConvertError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:             # `virtconvert formats | head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except OSError as exc:              # a full drive, a name too long…
        print(f"\nError: {os_error_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
