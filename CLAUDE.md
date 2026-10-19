# VirtConvert&Create — repository notes for Claude

Converts virtual disks (VDI, VMDK, VHD, VHDX, QCOW2, QED, OVA, raw; reads Parallels, DMG, ISO, QCOW, OVF too;
checkpoints merged), creates new ones (empty, partitioned, formatted with mkfs, or copied from a physical disk) and
writes them onto physical disks: the `virtconvert` command (no arguments → the PySide6 app). Called VirtConvert in 1.0; a 1.1 build was briefly misspelt VertConvert&Create
(command `vertconvert`): installers, PKGBUILD and settings clean that one up.
Python ≥ 3.10. qemu-img does the copying; VirtConvert&Create reads headers, merges differencing chains, builds new
disks and checks results. Primary target Arch Linux (PKGBUILD); `packaging/install.sh` covers the other
distributions. Design notes: docs/HOW-IT-WORKS.md.

## Layout
- `virtconvert/core/` — stdlib only, no Qt imports:
  `formats.py` (Format table, `GUI_TARGETS`; defaults: VirtualBox formats → VHDX, everything else → VDI),
  `probe.py` (detect format, read headers → `ImageInfo`; `member` = a disk inside an OVA), `vhd.py` / `vhdx.py` /
  `vdi.py` (headers + `Layer.map()` block maps, differencing included), `layers.py` (DATA/ZERO/PARENT extents,
  `RawLayer`), `chain.py` (find parents, `ChainReader`), `nbd.py` (read-only NBD server for qemu-img), `ova.py`
  (OVF parsing, OVA members by tar offset, `write_ova`), `qemu.py` (run qemu-img, `json:` specs incl.
  `member_spec`/`image_spec`, progress, cancel), `images.py` (`inspect_image` = probe + chain + partitions;
  `expand_sources` = OVA members / OVF files / .hdd bundle), `partitions.py` (read; `gpt_table`/`mbr_table`
  write), `convert.py` (`Target`, `Job`, `make_plan`, `execute` → `write_image` shared with create,
  `verify_images`, `repair`), `create.py` (`FILESYSTEMS`, `NewDisk`, `make_create_plan`, `create_disk`),
  `system.py` (doctor, distro package names for qemu-img and the mkfs tools, `find_program` incl. sbin),
  `devices.py` (physical disks: `list_disks` from lsblk, `inspect_device` → `ImageInfo.device`, `open_device`
  with the pkexec/sudo fd-passing helper (read-only, or `write=True`: O_RDWR|O_EXCL), `DeviceReader`,
  `DeviceWriter`, `disks_holding`, `unmount`; some partitions only: `plan_partition_copy` → `PartitionCopy`
  (`ImageInfo.layout`), `PartitionReader` builds a new GPT/MBR from the disk's own entries
  (`partitions.read_entries`)), `writedisk.py` (`DiskWrite`, `make_write_plan`, `write_to_disk`:
  an image onto a physical disk through the writable NBD server, verify, `partitions.move_backup_gpt`).
- `virtconvert/gui/` — PySide6. `main_window.py` (toolbar Add/New disk/Write to disk/Convert; a sidebar shown
  once the list has images: Image summary, Convert to with Compare and an Options menu; the queue table; the
  Activity log folded away behind the status bar's button, unfolding on errors), `pickers.py` (`TargetPicker`: 2×4 format grid + variants, used by both windows),
  `create_dialog.py` (New disk, `DiskChooser`), `write_dialog.py` (Write to disk), `queue.py` (`Item` incl. `member`, `QueueModel`, `StatusDelegate`),
  `dialogs.py`; `theme.py`, `icons.py`, `widgets.py`, `worker.py` are the suite's shared design system
  (EZP2019Linux).
- `virtconvert/cli.py` — sub-commands info/convert/create/write/disks/verify/repair/formats/doctor.
- `tests/imagewriter.py` writes test images from the specs; `tools/screenshot.py` makes docs/screenshots;
  `tools/installer-smoke.sh` is the per-distro installer test CI runs in containers.

## Invariants / gotchas
- Never write to a source. The only exception is `repair()` (explicit, confirmed by the user).
- The only writes to a physical disk are `write_to_disk` after the person confirmed erasing that disk (the app:
  `ask_yes_no` with Cancel as default; the CLI: typing the disk's name, or `--yes`). `make_write_plan` refuses the
  system disk, partitions, read-only disks, disks with holders (LVM/RAID/crypt), the disk the image is stored on,
  and a sector-size mismatch; `DiskWrite.expect` (`disk_identity`: size, model, serial, diskseq) catches a different
  disk under the name, checked again after the exclusive open. Writing less than the image (a smaller disk) needs
  known partition positions (sysfs `start`), is re-checked from the source's own table, and an unfittable GPT fails.
- Always pass formats explicitly (`qemu.source_spec`): qemu-img takes fixed VHDs for raw, and sizes VHDs from
  unknown creators by CHS (we force `force_size_calc=current_size`; VHD targets get `force_size=on`).
- qemu-img silently ignores the parent of a differencing VHD and refuses differencing VHDX/VDI:
  `ImageInfo.needs_chain_reader` → `ChainReader` + `NbdServer`. Parents are accepted only with a matching ID
  (VHD unique id, VHDX DataWriteGuid via parent_linkage/parent_linkage2, VDI uuid); IDs are GUIDs (bytes_le).
- Outputs are written into a per-run work folder `<name>.<random>.part/<name>`, verified, then published with
  `os.link` (no clobber) or `os.replace` (overwrite allowed); every failure path deletes the folder, except a
  name taken during the run (the checked copy is kept and reported). Multi-file VMDKs: every file is published,
  data files first, descriptor last, with rollback; `Target.output_files` predicts the names (qemu's naming).
- OVA: written from a stream VMDK made in `work/appliance/`, verified through `member_spec` inside the archive.
- Physical disks: read only through a file descriptor from the root helper (never qemu-img as root), served via
  NBD (`plan.uses_reader` is always true for devices). Never save onto the source disk (`_device_checks`); a
  mounted disk gets `plan.can_verify = False`. With `Job.partitions` only the chosen partitions are read (their
  positions from sysfs, checked against the disk's own table once open); the image may go onto another partition of
  the same disk, never onto a chosen one (`partitions_holding`).
- New disks: sparse raw scratch + `mkfs` on a partition-sized sparse file + `copy_allocated`; empty disks use
  `qemu-img create`. VHDX targets are verified with the own reader (`compare_with_reader` + `stored_ranges`),
  since qemu's VHDX driver has no block status and `qemu-img compare` would read every byte.
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
./virtconvert.sh     # the app
virtconvert info some.vhdx
```
