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
from .core import formats as F
from .core import system
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
from .core.errors import ConvertError, OperationCancelled, QemuImgMissingError
from .core.images import inspect_image
from .core.probe import ImageInfo
from .core.qemu import QemuImg
from .core.util import human_bytes, mmss

SPINNER = "|/-\\"
COMMANDS = ("gui", "info", "convert", "verify", "repair", "formats", "doctor")


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
        "path": str(info.path), "format": info.format.key if info.format else None,
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
    infos = [inspect_image(Path(f), qemu, candidates=args.parent) for f in args.files]
    if args.json:
        print(json.dumps([_info_json(i) for i in infos] if len(infos) > 1 else _info_json(infos[0]),
                         indent=2))
        return 0 if all(i.ok for i in infos) else 1
    for n, info in enumerate(infos):
        if n:
            print()
        print(info.path)
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
    sources = [Path(f) for f in args.files]
    if args.output and len(sources) > 1:
        raise SystemExit("--output works with one source; use --dir for several.")
    if args.dir and not Path(args.dir).is_dir():
        raise SystemExit(f"{args.dir} is not a folder.")
    candidates = [Path(p) for p in args.parent]
    failures = 0
    for n, source in enumerate(sources):
        if n:
            print()
        parents = {}
        if args.force_parent and len(candidates) == 1:
            parents = {source: candidates[0]}
        info = inspect_image(source, qemu, parents, args.force_parent, candidates)
        target = parse_target(args.to, fixed=args.fixed) if args.to else default_target(info)
        if args.fixed and not target.fixed and not target.format.can_be_fixed:
            print(f"  ! {target.format.label} has no fixed variant; making a dynamic one.")
        if args.output:
            output = Path(args.output)
        else:
            output = default_output(info, target, Path(args.dir) if args.dir else None,
                                    unique=False)
        job = Job(source=source, target=target, output=output, verify=not args.no_verify,
                  overwrite=args.overwrite, parents=parents, force_parent=args.force_parent,
                  candidates=candidates)
        plan = make_plan(job, qemu, info)
        print(f"{source.name} → {job.output}")
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
        print(f"  ✓ {outcome.output.name}: {human_bytes(outcome.file_size)} file{check} "
              f"({mmss(outcome.seconds)})")
    return 1 if failures else 0


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
    print("Formats (read and write):")
    for fmt in F.ALL:
        flag = ""
        if qemu is not None and qemu.formats and fmt.driver not in qemu.formats:
            flag = "  (not supported by this qemu-img)"
        print(f"  {fmt.key:<6} {fmt.label:<5} {', '.join(fmt.extensions):<16} {fmt.title}{flag}")
    print("\nTargets for --to (formats, or these shortcuts):")
    shortcuts = {"virtualbox": "VDI, dynamic", "hyperv": "VHDX, dynamic", "vmware": "VMDK, dynamic",
                 "ova": "VMDK, stream-optimized (also: esxi)", "azure": "VHD, fixed, whole MB"}
    for name, text in shortcuts.items():
        assert name in PRESETS
        print(f"  {name:<11} {text}")
    print("\nWithout --to: VHD and VHDX become VDI (VirtualBox), everything else becomes VHDX (Hyper-V).")
    return 0


def cmd_doctor(args) -> int:
    print(f"VertConvert&Create {__version__} on {system.distro_name()}")
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
        print("or run:  vertconvert doctor --install-qemu")
    return 1 if problems else 0


# ---------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vertconvert",
        description="Convert Hyper-V disks (VHD, VHDX) to VirtualBox (VDI, VMDK) and back. "
                    "Without a command the app starts.",
        epilog="Examples:\n"
               "  vertconvert                          open the app\n"
               "  vertconvert info disk.vhdx           show what an image contains\n"
               "  vertconvert convert disk.vhdx        -> disk.vdi for VirtualBox\n"
               "  vertconvert convert disk.vdi         -> disk.vhdx for Hyper-V\n"
               "  vertconvert convert -t vmdk *.vhdx   several at once\n"
               "  vertconvert convert -t azure os.vdi  fixed VHD for Azure\n"
               "  vertconvert verify disk.vhdx disk.vdi\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"vertconvert {__version__}")
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
                       description="Convert images. Without --to, VHD and VHDX files become VDI "
                                   "and everything else becomes VHDX.")
    p.add_argument("files", nargs="+", metavar="IMAGE")
    p.add_argument("-t", "--to", metavar="FORMAT",
                   help="vdi, vmdk, vhd, vhdx, qcow2, raw, or virtualbox, hyperv, vmware, ova, azure")
    p.add_argument("-o", "--output", metavar="FILE", help="the converted file (one source only)")
    p.add_argument("-d", "--dir", metavar="FOLDER", help="put converted files in this folder")
    p.add_argument("--fixed", action="store_true",
                   help="allocate the whole disk now (VDI, VHD, VHDX, QCOW2)")
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

    p = sub.add_parser("doctor", help="check that everything VertConvert&Create needs is installed")
    p.add_argument("--install-qemu", action="store_true",
                   help="install qemu-img with your package manager (asks for your password)")
    p.set_defaults(func=cmd_doctor)
    return ap


def _split_files(argv: list[str]) -> tuple[list[str], list[str]]:
    """`vertconvert disk.vhdx` opens the app with the file; tell files from commands."""
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
                  "Install it (see the README), or use the commands: vertconvert --help",
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


if __name__ == "__main__":
    sys.exit(main())
