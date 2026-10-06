"""Everything the app and the CLI show about an image: headers, parents, partitions."""

from __future__ import annotations

from pathlib import Path

from . import ova
from .chain import LAYERS, open_layer, read_head, resolve_chain
from .errors import ConvertError
from .partitions import analyze
from .probe import ImageInfo, probe
from .qemu import QemuImg, image_spec

HEAD_BYTES = 128 * 1024


def expand_sources(path: str | Path) -> list[tuple[Path, str | None]]:
    """The disks a file stands for: each disk of an OVA, the disk files an OVF lists, the
    image in a Parallels .hdd folder, or the file itself."""
    path = Path(path)
    lowered = path.name.lower()
    try:
        if path.is_dir() and lowered.endswith(".hdd"):
            images = sorted(path.glob("*.hds"))
            if len(images) == 1:
                return [(images[0], None)]
        elif path.is_file() and lowered.endswith(".ova"):
            disks = ova.list_ova(path)
            if len(disks) > 1:
                return [(path, d.name) for d in disks]
        elif path.is_file() and lowered.endswith(".ovf"):
            files = ova.list_ovf(path)
            if files:
                return [(f, None) for f in files]
    except ConvertError:
        pass       # probe() reports the problem
    return [(path, None)]


def inspect_image(path: str | Path, qemu: QemuImg | None = None,
                  parents: dict[Path, Path] | None = None, force: bool = False,
                  candidates: list[Path] | None = None, member: str | None = None) -> ImageInfo:
    """Probe PATH, find the parents of a differencing image and read its partition table.

    PARENTS maps images to parents chosen by hand; FORCE accepts a parent whose
    ID doesn't match; CANDIDATES are extra files to look at when searching for
    parents. Never raises for a bad image: see info.errors.
    """
    info = probe(path, member)
    if info.format is None or info.format.container:
        return info
    resolve_chain(info, parents, force, candidates)
    for image in [info] + info.chain:
        if image.format in LAYERS and image.ok and not image.member:
            try:
                open_layer(image).close()
            except ConvertError as exc:
                if str(exc) not in info.errors:
                    info.errors.append(str(exc))
    head = read_head(info, HEAD_BYTES)
    if head is None and qemu is not None and info.ok and not info.needs_chain_reader:
        try:
            head = qemu.read_head(image_spec(info), HEAD_BYTES)
        except Exception:  # noqa: BLE001 - the partition table is only extra information
            head = None
    if head:
        info.partitions = analyze(head, info.logical_sector if info.logical_sector != 512 else None)
    return info
