"""Everything the app and the CLI show about an image: headers, parents, partitions."""

from __future__ import annotations

from pathlib import Path

from .chain import read_head, resolve_chain
from .partitions import analyze
from .probe import ImageInfo, probe
from .qemu import QemuImg, source_spec

HEAD_BYTES = 128 * 1024


def inspect_image(path: str | Path, qemu: QemuImg | None = None,
                  parents: dict[Path, Path] | None = None, force: bool = False,
                  candidates: list[Path] | None = None) -> ImageInfo:
    """Probe PATH, find the parents of a differencing image and read its partition table.

    PARENTS maps images to parents chosen by hand; FORCE accepts a parent whose
    ID doesn't match; CANDIDATES are extra files to look at when searching for
    parents. Never raises for a bad image: see info.errors.
    """
    info = probe(path)
    if info.format is None:
        return info
    resolve_chain(info, parents, force, candidates)
    head = read_head(info, HEAD_BYTES)
    if head is None and qemu is not None and info.ok and not info.needs_chain_reader:
        try:
            head = qemu.read_head(source_spec(info.path, info.format.driver), HEAD_BYTES)
        except Exception:  # noqa: BLE001 - the partition table is only extra information
            head = None
    if head:
        info.partitions = analyze(head, info.logical_sector if info.logical_sector != 512 else None)
    return info
