"""Differencing images: find the parents and read the merged disk.

Hyper-V checkpoints (.avhd/.avhdx) and VirtualBox snapshots only store what
changed since their parent. To convert one, its whole chain is read as a
single disk: each part of the disk comes from the newest image that has it.
qemu-img can't do this for VHD, VHDX and VDI, so VirtConvert&Create does the reading
and hands qemu-img the merged disk (see nbd.py).
"""

from __future__ import annotations

import os
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

from . import formats as F
from .errors import ImageFormatError, ParentNotFoundError
from .layers import DATA, PARENT, Layer, RawLayer
from .probe import ImageInfo, probe
from .util import allocated_bytes, find_case_insensitive, same_file
from .vdi import VdiLayer
from .vhd import VhdLayer
from .vhdx import VhdxLayer

MAX_DEPTH = 128
LAYERS = {F.VHD: VhdLayer, F.VHDX: VhdxLayer, F.VDI: VdiLayer, F.RAW: RawLayer}


def normalize_id(value: str | None) -> str:
    return (value or "").strip().strip("{}").lower()


def open_layer(info: ImageInfo) -> Layer:
    cls = LAYERS.get(info.format)
    if cls is None:
        raise ImageFormatError(f"{info.name}: VirtConvert&Create can't read {info.label} images itself")
    return cls(info.path)


def _hint_paths(child: Path, hint: str) -> list[Path]:
    """Places a parent path stored by Windows or VirtualBox may point to on this system."""
    text = hint.strip().replace("\\", "/")
    if text.startswith("file://"):
        text = text[7:]
    candidates: list[Path] = []
    name = text.rsplit("/", 1)[-1]
    is_windows_abs = len(text) > 2 and text[1] == ":" or text.startswith("//")
    if text.startswith("/") and not text.startswith("//"):
        candidates.append(Path(text))
    elif not is_windows_abs:
        candidates.append(child.parent / text)
    for directory in (child.parent, child.parent.parent):
        if name:
            candidates.append(directory / name)
    return candidates


def vbox_registry(child: Path) -> dict[str, Path]:
    """uuid -> path from the .vbox files of the VM the image belongs to."""
    result: dict[str, Path] = {}
    dirs = [child.parent, child.parent.parent]
    for directory in dirs:
        try:
            files = [p for p in directory.iterdir() if p.suffix.lower() in (".vbox", ".vbox-prev")]
        except OSError:
            continue
        for vbox in files:
            try:
                root = ET.parse(vbox).getroot()
            except (ET.ParseError, OSError):
                continue
            for el in root.iter():
                if el.tag.rsplit("}", 1)[-1] == "HardDisk" and el.get("uuid") and el.get("location"):
                    location = Path(el.get("location"))
                    if not location.is_absolute():
                        location = vbox.parent / location
                    result.setdefault(normalize_id(el.get("uuid")), location)
    return result


def _scan(child: ImageInfo) -> list[Path]:
    """Images of the same format next to the child, in its parent folder and in Snapshots."""
    found: list[Path] = []
    folders = [child.path.parent, child.path.parent.parent, child.path.parent / "Snapshots"]
    exts = child.format.extensions if child.format else ()
    for folder in folders:
        try:
            for entry in os.scandir(folder):
                if entry.is_file() and entry.name.lower().endswith(exts):
                    found.append(Path(entry.path))
        except OSError:
            continue
    return found


def parent_matches(child: ImageInfo, parent: ImageInfo) -> bool:
    if parent.format is not child.format:
        return False
    wanted = {normalize_id(child.parent_identity)} | {normalize_id(x) for x in child.parent_identity_alt}
    wanted.discard("")
    return normalize_id(parent.identity) in wanted


def find_parent(child: ImageInfo, override: Path | None = None,
                candidates: list[Path] | None = None) -> tuple[ImageInfo | None, str]:
    """Return (parent, problem). A wrong override still comes back, with the problem.

    CANDIDATES are files to try first (they must have the right ID)."""
    if override is not None:
        parent = probe(override)
        if not parent.ok:
            return None, "; ".join(parent.errors)
        if not parent_matches(child, parent):
            return parent, (f"{parent.name} is not the parent {child.name} was made from "
                            "(its ID doesn't match).")
        return parent, ""
    seen: set[Path] = set()
    mismatched: list[str] = []

    def consider(path: Path) -> ImageInfo | None:
        if path in seen or same_file(path, child.path):
            return None
        seen.add(path)
        if not path.is_file():
            found = find_case_insensitive(path.parent, path.name)
            if found is None or found in seen:
                return None
            path = found
            seen.add(path)
        parent = probe(path)
        if parent.format is child.format and parent_matches(child, parent):
            return parent
        if parent.format is child.format:
            mismatched.append(path.name)
        return None

    for candidate in candidates or []:
        parent = consider(Path(candidate))
        if parent:
            return parent, ""
    for hint in child.parent_hints:
        for candidate in _hint_paths(child.path, hint):
            parent = consider(candidate)
            if parent:
                return parent, ""
    registry = vbox_registry(child.path)
    for key in [normalize_id(child.parent_identity)] + [normalize_id(x) for x in child.parent_identity_alt]:
        if key in registry:
            parent = consider(registry[key])
            if parent:
                return parent, ""
    for candidate in _scan(child):
        parent = consider(candidate)
        if parent:
            return parent, ""
    names = ", ".join(dict.fromkeys(Path(h.replace("\\", "/")).name for h in child.parent_hints))
    problem = f"The parent of {child.name}"
    problem += f" ({names})" if names else f" (ID {child.parent_identity})"
    problem += " was not found. Put it in the same folder, or choose it with “Choose parent…”."
    if mismatched:
        problem += (f" {', '.join(dict.fromkeys(mismatched))} has the right name but a different ID: "
                    "it was changed after this checkpoint was made, so merging them would "
                    "give a damaged disk.")
    return None, problem


def resolve_chain(info: ImageInfo, overrides: dict[Path, Path] | None = None,
                  force: bool = False, candidates: list[Path] | None = None) -> None:
    """Fill info.chain with the parents of a differencing image (nearest first).

    OVERRIDES maps an image to the parent the user picked. FORCE accepts a
    parent whose ID doesn't match (a warning instead of an error).
    """
    info.chain = []
    info.chain_size = info.allocated
    if not info.differencing or not info.needs_chain_reader:
        return
    overrides = overrides or {}
    current = info
    visited = {info.path.resolve()}
    while current.differencing:
        if len(info.chain) >= MAX_DEPTH:
            info.errors.append("The chain of parents is too long (a loop?).")
            return
        override = next((p for c, p in overrides.items() if same_file(c, current.path)), None)
        parent, problem = find_parent(current, override, candidates)
        if parent is None:
            info.errors.append(problem)
            return
        if problem:
            if force:
                info.warnings.append(problem + " Used anyway, as you asked.")
            else:
                info.errors.append(problem)
                return
        key = parent.path.resolve()
        if key in visited:
            info.errors.append(f"{parent.name} appears twice in the chain of parents.")
            return
        visited.add(key)
        if parent.errors:
            info.errors.extend(parent.errors)
            return
        info.warnings.extend(w for w in parent.warnings if w not in info.warnings)
        info.chain.append(parent)
        info.chain_size += parent.allocated
        current = parent
    base = info.chain[-1] if info.chain else None
    if base is not None and base.virtual_size != info.virtual_size:
        info.notes.append(f"The disk was resized after {base.name} was made "
                          f"({base.virtual_size:,} → {info.virtual_size:,} bytes).")


class ChainReader:
    """Reads a differencing image together with its parents as one disk."""

    def __init__(self, infos: list[ImageInfo], size: int | None = None):
        """SIZE, if larger than the disk, adds zeros at the end."""
        self.layers: list[Layer] = []
        try:
            for info in infos:
                self.layers.append(open_layer(info))
        except Exception:
            self.close()
            raise
        self.size = self.layers[0].size if size is None else size
        self._lock = threading.Lock()

    @classmethod
    def for_image(cls, info: ImageInfo, size: int | None = None) -> "ChainReader":
        return cls([info] + info.chain, size)

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self.size - offset))
        buf = bytearray(length)
        if length:
            with self._lock:
                self._fill(0, offset, length, buf, 0)
        return bytes(buf)

    def _fill(self, level: int, offset: int, length: int, buf: bytearray, pos: int) -> None:
        layer = self.layers[level]
        length = min(length, layer.size - offset)  # past a smaller parent's end: zeros
        if length <= 0:
            return
        for kind, n, file_offset in layer.map(offset, length):
            if kind == DATA:
                buf[pos:pos + n] = layer.pread(n, file_offset)
            elif kind == PARENT and level + 1 < len(self.layers):
                self._fill(level + 1, offset, n, buf, pos)
            offset += n
            pos += n

    def stored_bytes(self) -> int:
        return sum(allocated_bytes(layer.path) for layer in self.layers)

    def close(self) -> None:
        for layer in self.layers:
            layer.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_head(info: ImageInfo, length: int = 128 * 1024) -> bytes | None:
    """The first bytes of the virtual disk, if VirtConvert&Create can read the image itself."""
    if info.format not in LAYERS or not info.ok or info.member:
        return None
    if info.differencing and (not info.chain or info.chain[-1].differencing):
        return None
    try:
        with ChainReader.for_image(info) as reader:
            return reader.read(0, min(length, reader.size))
    except (OSError, ImageFormatError, ParentNotFoundError):
        return None
    except Exception:  # noqa: BLE001 - a damaged file must not break inspection
        return None
