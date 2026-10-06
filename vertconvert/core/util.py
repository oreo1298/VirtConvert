"""Small helpers shared by the core, CLI and GUI."""

from __future__ import annotations

import os
from pathlib import Path

KIB = 1 << 10
MIB = 1 << 20
GIB = 1 << 30
TIB = 1 << 40


def human_bytes(n: int | float, precise: bool = False) -> str:
    """Binary units with the short names Hyper-V and VirtualBox show (64 GB = 64 GiB)."""
    n = float(n)
    for unit, size in (("TB", TIB), ("GB", GIB), ("MB", MIB), ("KB", KIB)):
        if abs(n) >= size:
            value = n / size
            if precise or value < 10:
                text = f"{value:.2f}"
            elif value < 100:
                text = f"{value:.1f}"
            else:
                text = f"{value:.0f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{int(n)} B"


def mmss(seconds: float) -> str:
    seconds = max(0, int(seconds + 0.5))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def round_up(value: int, multiple: int) -> int:
    return -(-value // multiple) * multiple


def allocated_bytes(path: str | os.PathLike) -> int:
    """Space the file takes on disk (sparse files take less than their length)."""
    try:
        st = os.stat(path)
    except OSError:
        return 0
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if blocks is not None else st.st_size


def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return Path(os.path.abspath(a)) == Path(os.path.abspath(b))


def file_signature(path: Path) -> tuple[int, int] | None:
    """(size, mtime) used to notice a source changing while it is being converted."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def find_case_insensitive(directory: Path, name: str) -> Path | None:
    """Find NAME in DIRECTORY ignoring case (Windows paths in image headers)."""
    candidate = directory / name
    if candidate.is_file():
        return candidate
    try:
        lowered = name.lower()
        for entry in os.scandir(directory):
            if entry.name.lower() == lowered and entry.is_file():
                return Path(entry.path)
    except OSError:
        pass
    return None
