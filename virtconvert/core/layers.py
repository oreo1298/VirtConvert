"""Read access to one image file, as a map from virtual offsets to file contents.

A layer answers "where is the data for this part of the virtual disk?" with
DATA (at a file offset), ZERO, or PARENT (ask the image this one is based on).
The chain reader in chain.py stacks layers to read differencing images
(Hyper-V checkpoints, VirtualBox snapshots), which qemu-img cannot do for VHD,
VHDX and VDI.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path
from typing import Iterator

from .errors import ImageFormatError

DATA = "data"
ZERO = "zero"
PARENT = "parent"

Extent = tuple[str, int, int]  # (kind, length, file offset)


class LRU(OrderedDict):
    def __init__(self, capacity: int):
        super().__init__()
        self.capacity = capacity

    def get_or(self, key, factory):
        if key in self:
            self.move_to_end(key)
            return self[key]
        value = factory()
        self[key] = value
        if len(self) > self.capacity:
            self.popitem(last=False)
        return value


def bit_runs(bitmap: bytes, first: int, count: int, msb_first: bool) -> list[tuple[int, int, bool]]:
    """Runs of equal bits [(start, end, is_set)] for bits first..first+count-1."""
    runs: list[tuple[int, int, bool]] = []
    start = first
    current = None
    end = first + count
    i = first
    while i < end:
        byte = bitmap[i >> 3]
        # whole bytes that are all 0 or all 1 are common: skip them quickly
        if i & 7 == 0 and i + 8 <= end and byte in (0, 0xFF):
            value = byte == 0xFF
            if current is None:
                current = value
            elif value != current:
                runs.append((start, i, current))
                start, current = i, value
            i += 8
            continue
        shift = 7 - (i & 7) if msb_first else (i & 7)
        value = bool((byte >> shift) & 1)
        if current is None:
            current = value
        elif value != current:
            runs.append((start, i, current))
            start, current = i, value
        i += 1
    if current is not None:
        runs.append((start, end, current))
    return runs


class Layer:
    """One image file. Subclasses fill in the geometry and implement map()."""

    format_label = "?"

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        try:
            self.fd = os.open(self.path, flags)
        except OSError as exc:
            raise ImageFormatError(f"Cannot open {self.path}: {exc.strerror or exc}") from exc
        self.file_size = os.fstat(self.fd).st_size
        self.size = 0
        self.differencing = False
        self.identity: str | None = None          # what a child refers to
        self.parent_identity: str | None = None   # what this image refers to
        self.parent_hints: list[str] = []         # paths stored in the header

    def map(self, offset: int, length: int) -> Iterator[Extent]:
        raise NotImplementedError

    def pread(self, length: int, offset: int) -> bytes:
        data = os.pread(self.fd, length, offset)
        if len(data) < length:
            # reading past the end of a truncated file: the rest reads as zeros
            data += bytes(length - len(data))
        return data

    def allocated_extents(self) -> Iterator[tuple[int, int]]:
        """(virtual offset, length) of the parts stored in this file."""
        pos = 0
        step = 64 << 20
        while pos < self.size:
            n = min(step, self.size - pos)
            off = pos
            for kind, length, _foff in self.map(pos, n):
                if kind == DATA:
                    yield off, length
                off += length
            pos += n

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - never raise from a finalizer
            pass


class RawLayer(Layer):
    format_label = "raw"

    def __init__(self, path, size: int | None = None):
        super().__init__(path)
        self.size = self.file_size if size is None else size

    def map(self, offset: int, length: int) -> Iterator[Extent]:
        if length > 0:
            yield DATA, length, offset
