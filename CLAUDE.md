# VertConvert&Create — repository notes for Claude

Converts Hyper-V disks (VHD, VHDX, checkpoints) to VirtualBox formats (VDI, VMDK) and back: the `vertconvert`
command (no arguments → the PySide6 app). Python ≥ 3.10. qemu-img does the copying; VertConvert&Create reads headers,
merges differencing chains and checks results. Primary target Arch Linux (PKGBUILD); `packaging/install.sh`
covers the other distributions. Design notes: docs/HOW-IT-WORKS.md.

## Layout
- `vertconvert/core/` — stdlib only, no Qt imports:
  `formats.py` (Format table, defaults: Hyper-V formats → VDI, others → VHDX), `probe.py` (detect format,
  read headers → `ImageInfo`), `vhd.py` / `vhdx.py` / `vdi.py` (headers + `Layer.map()` block maps,
  differencing included), `layers.py` (DATA/ZERO/PARENT extents, `RawLayer`), `chain.py` (find parents,
  `ChainReader`), `nbd.py` (read-only NBD server for qemu-img), `qemu.py` (run qemu-img, `json:` specs,
  progress, cancel), `images.py` (`inspect_image` = probe + chain + partitions), `partitions.py`,
  `convert.py` (`Target`, `Job`, `make_plan`, `execute`, `verify_images`, `repair`), `system.py` (doctor,
  distro package names for qemu-img).
- `vertconvert/gui/` — PySide6. `main_window.py` (toolbar, Image / Convert to / Options cards, queue table,
  Activity log, status bar), `queue.py` (`Item`, `QueueModel`, `StatusDelegate`), `dialogs.py`; `theme.py`,
  `icons.py`, `widgets.py`, `worker.py` are the suite's shared design system (EZP2019Linux).
- `vertconvert/cli.py` — sub-commands info/convert/verify/repair/formats/doctor.
- `tests/imagewriter.py` writes test images from the specs; `tools/screenshot.py` makes docs/screenshots;
  `tools/installer-smoke.sh` is the per-distro installer test CI runs in containers.

## Invariants / gotchas
- Never write to a source. The only exception is `repair()` (explicit, confirmed by the user).
- Always pass formats explicitly (`qemu.source_spec`): qemu-img takes fixed VHDs for raw, and sizes VHDs from
  unknown creators by CHS (we force `force_size_calc=current_size`; VHD targets get `force_size=on`).
- qemu-img silently ignores the parent of a differencing VHD and refuses differencing VHDX/VDI:
  `ImageInfo.needs_chain_reader` → `ChainReader` + `NbdServer`. Parents are accepted only with a matching ID
  (VHD unique id, VHDX DataWriteGuid via parent_linkage/parent_linkage2, VDI uuid); IDs are GUIDs (bytes_le).
- Outputs are written into a per-run work folder `<name>.<random>.part/<name>`, verified, then published with
  `os.link` (no clobber) or `os.replace` (overwrite allowed); every failure path deletes the folder, except a
  name taken during the run (the checked copy is kept and reported).
- Readers check block tables against the file size and `Layer.pread` raises on short reads: never read a cut-off
  file as zeros. VHDX with 4 KB logical sectors always goes through the reader (qemu's driver is 512-only).
- VHDX outputs get `vhdx.mark_regions_required()` (qemu leaves the flag clear; VirtualBox then refuses them).
- Growing a target (Azure → whole MiB, stream VMDK → 64 KiB grains): through the NBD reader when we can read
  the source, else `qemu-img create` + `convert -n --target-is-zero`.
- The GUI never blocks: inspection runs in a thread pool (`inspected` signal), conversions in `OperationRunner`.
- Two queue items must not share an output (`default_output(..., taken=...)` and the clash check in `_replan`).

## Running
```sh
python -m venv --system-site-packages .venv && . .venv/bin/activate
pip install -e ".[dev]"
make test            # QT_QPA_PLATFORM=offscreen pytest (needs qemu-img; VirtualBox/libvhdi tests skip without them)
make lint            # ruff + bash -n
./vertconvert.sh     # the app
vertconvert info some.vhdx
```
