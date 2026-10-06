"""Running qemu-img: conversion, comparison, repair and image creation.

Sources are always passed as `json:` specifications with an explicit driver,
so qemu-img never guesses the format (it takes fixed VHD files for raw images)
and file names are never mistaken for protocols or options.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .errors import OperationCancelled, QemuImgError, QemuImgMissingError

Progress = Callable[[float], None]       # 0..100
Cancel = Callable[[], bool]

_PROGRESS_RE = re.compile(rb"\((\d+(?:\.\d+)?)/100%\)")
_VERSION_RE = re.compile(r"version\s+(\d+)\.(\d+)(?:\.(\d+))?")
MIN_VERSION = (2, 10)


def spec(path: str | os.PathLike, driver: str, **options) -> str:
    """A json: image specification for qemu-img."""
    data: dict = {"driver": driver}
    data.update(options)
    data["file"] = {"driver": "file", "filename": os.path.abspath(path)}
    return "json:" + json.dumps(data)


def source_spec(path: str | os.PathLike, driver: str) -> str:
    if driver == "vpc":
        # the full size from the footer, like Hyper-V and VirtualBox (see vhd.Footer.virtual_size)
        return spec(path, "vpc", force_size_calc="current_size")
    return spec(path, driver)


def nbd_spec(socket_path: str, export: str) -> str:
    """The disk served by nbd.NbdServer, as a raw image."""
    return "json:" + json.dumps({"driver": "raw", "file": {
        "driver": "nbd", "server": {"type": "unix", "path": socket_path}, "export": export}})


def clean_error(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for prefix in ("qemu-img: ", "qemu-img:"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
        line = re.sub(r"json:\{[^}]*\"filename\": \"([^\"]+)\"[^}]*\}+", r"\1", line)
        # "Could not open 'x': Could not open 'x': reason" -> "Could not open 'x': reason"
        line = re.sub(r"^(Could not open '[^']*': )+(?=Could not open)", "", line)
        lines.append(line)
    return "\n".join(dict.fromkeys(lines)) or "qemu-img failed"


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class QemuImg:
    path: str
    version: tuple[int, int, int] = (0, 0, 0)
    version_text: str = ""
    formats: set[str] = field(default_factory=set)

    @classmethod
    def find(cls) -> "QemuImg | None":
        exe = (os.environ.get("VERTCONVERT_QEMU_IMG") or os.environ.get("VIRTCONVERT_QEMU_IMG")
               or shutil.which("qemu-img"))
        if not exe:
            for candidate in ("/usr/bin/qemu-img", "/usr/local/bin/qemu-img", "/run/current-system/sw/bin/qemu-img"):
                if os.access(candidate, os.X_OK):
                    exe = candidate
                    break
        if not exe:
            return None
        q = cls(exe)
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20,
                                 env=_env()).stdout
            m = _VERSION_RE.search(out)
            if m:
                q.version = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
                q.version_text = ".".join(str(x) for x in q.version)
            help_text = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20,
                                       env=_env()).stdout
            m = re.search(r"Supported formats:(.*)", help_text)
            if m:
                q.formats = set(m.group(1).split())
        except (OSError, subprocess.SubprocessError):
            return None
        return q

    @classmethod
    def require(cls) -> "QemuImg":
        q = cls.find()
        if q is None:
            raise QemuImgMissingError()
        return q

    @property
    def too_old(self) -> bool:
        return self.version[:2] < MIN_VERSION

    def missing_drivers(self, drivers=("vpc", "vhdx", "vdi", "vmdk", "nbd")) -> list[str]:
        if not self.formats:
            return []
        return [d for d in drivers if d not in self.formats]

    # ------------------------------------------------------------------ running

    def run(self, args: list[str], progress: Progress | None = None, cancel: Cancel | None = None,
            keep_output: bool = True) -> Result:
        """Run qemu-img; reports `-p` progress and stops it when CANCEL returns True."""
        proc = subprocess.Popen([self.path, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=_env(), start_new_session=True)
        out, err = bytearray(), bytearray()
        tail = b""
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, "out")
        sel.register(proc.stderr, selectors.EVENT_READ, "err")
        streams = 2
        try:
            while streams:
                if cancel is not None and cancel():
                    _stop(proc)
                    raise OperationCancelled()
                for key, _ in sel.select(timeout=0.15):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        streams -= 1
                        continue
                    if key.data == "err":
                        err += chunk
                        continue
                    if progress is not None:
                        found = _PROGRESS_RE.findall(tail + chunk)
                        if found:
                            progress(float(found[-1]))
                        tail = (tail + chunk)[-32:]
                    if keep_output:
                        out += chunk
            while proc.poll() is None:
                if cancel is not None and cancel():
                    _stop(proc)
                    raise OperationCancelled()
                try:
                    proc.wait(timeout=0.15)
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            if proc.poll() is None:
                _stop(proc)
            raise
        finally:
            sel.close()
            proc.stdout.close()
            proc.stderr.close()
        return Result(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

    def check_run(self, args: list[str], what: str, **kw) -> Result:
        res = self.run(args, **kw)
        if res.returncode != 0:
            raise QemuImgError(f"{what} failed: {clean_error(res.stderr or res.stdout)}")
        return res

    # ------------------------------------------------------------------ commands

    def info(self, image_spec: str) -> dict:
        res = self.check_run(["info", "--output=json", image_spec], "Reading the image")
        return json.loads(res.stdout)

    def create(self, path: Path, driver: str, size: int, options: dict[str, str]) -> None:
        args = ["create", "-q", "-f", driver]
        if options:
            args += ["-o", _opts(options)]
        self.check_run(args + [str(path), str(size)], "Creating the image")

    def convert(self, source: str, target: Path, driver: str, options: dict[str, str], *,
                progress: Progress | None = None, cancel: Cancel | None = None,
                existing_target: bool = False) -> None:
        args = ["convert", "-p"]
        if existing_target:
            # the target was created beforehand (to give it a different size): it reads as zeros
            args += ["-n", "--target-is-zero"]
        else:
            if options:
                args += ["-o", _opts(options)]
        args += [source, "-O", driver, str(target)]
        self.check_run(args, "Converting", progress=progress, cancel=cancel, keep_output=False)

    def compare(self, a: str, b: str, *, progress: Progress | None = None,
                cancel: Cancel | None = None) -> tuple[bool, str]:
        res = self.run(["compare", "-p", a, b], progress=progress, cancel=cancel)
        text = clean_error(_strip_progress(res.stdout) + "\n" + res.stderr)
        if res.returncode == 0:
            return True, "The images are identical."
        if res.returncode == 1:
            return False, text
        raise QemuImgError(f"Comparing failed: {text}")

    def check(self, path: Path, driver: str, repair: bool = False) -> Result:
        args = ["check"]
        if repair:
            args += ["-r", "all"]
        return self.run(args + [spec(path, driver)])

    def read_head(self, image_spec: str, length: int) -> bytes:
        """The first LENGTH bytes of the virtual disk."""
        with tempfile.TemporaryDirectory(prefix="vertconvert-") as tmp:
            out = os.path.join(tmp, "head.raw")
            block = 64 * 1024
            count = max(1, -(-length // block))
            self.check_run(["dd", "-O", "raw", f"bs={block}", f"count={count}",
                            f"if={image_spec}", f"of={out}"], "Reading the disk")
            with open(out, "rb") as f:
                return f.read(length)


def _opts(options: dict[str, str]) -> str:
    return ",".join(f"{k}={str(v).replace(',', ',,')}" for k, v in options.items())


def _strip_progress(text: str) -> str:
    return _PROGRESS_RE.sub(b"", text.encode()).decode().replace("\r", "\n")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return env


def _stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
