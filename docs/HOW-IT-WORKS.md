# How VirtConvert works

VirtConvert is a careful front end to `qemu-img`: it decides what the source is, finds everything
the source depends on, decides how to write the target, lets qemu-img do the copying, and checks
the result. This page describes each step and why it is there.

## 1. Recognising the image (`core/probe.py`)

qemu-img can guess formats, but it guesses from the first bytes of a file only. A **fixed VHD**
is raw data followed by a 512-byte footer, so qemu-img takes it for a raw image (and would then
copy the footer into the disk). VirtConvert therefore looks for:

| Format | Signature |
|---|---|
| VHDX | `vhdxfile` at offset 0 |
| VHD (dynamic, differencing) | `conectix` at offset 0 (copy of the footer) |
| VHD (fixed) | `conectix` in the last 512 (or 511, very old files) bytes |
| VDI | `0xBEDA107F` at offset 0x40 |
| VMDK | `KDMV` (sparse), `COWD` (ESX), or a `# Disk DescriptorFile` text descriptor |
| QCOW2 | `QFI\xfb` |
| Raw | by extension only (`.img`, `.raw`, `.bin`, `.dd`) |

and always passes the format to qemu-img explicitly, as a `json:` specification
(`{"driver": "vhdx", "file": {"driver": "file", "filename": …}}`), which also keeps unusual file
names from being read as qemu protocols or options.

It reads the headers it needs to show and to plan the conversion: sizes, block size, sector
size, IDs, the program that created the file, parent references, and problems (bad checksums,
a damaged VHDX header copy, a VHDX log that still has to be applied, a cut-off file).

### The size of a VHD

A VHD footer holds both a byte size (`current size`) and a CHS geometry. Virtual PC showed disks
at the geometry's size; Hyper-V, VirtualBox and everything newer use the byte size. qemu-img uses
the geometry for every file not made by a program on its short list, and so cuts the last few KB
off VHDs made by VirtualBox — where a GPT disk keeps its backup partition table. VirtConvert always
uses the byte size and tells qemu-img to do the same (`force_size_calc=current_size`). VHD files
VirtConvert writes are created with `force_size=on`, so their size is exact (Hyper-V and Azure
need that).

### Partition table and boot mode (`core/partitions.py`)

The first 128 KB of the virtual disk are read (by VirtConvert's own readers, or `qemu-img dd`)
and checked for a GPT (at 512-byte and 4 KB sectors) or an MBR. A GPT disk boots with UEFI
(VirtualBox: *Enable EFI*; Hyper-V: generation 2, which needs VHDX), an MBR disk with BIOS
(generation 1). A GPT disk with a *BIOS boot* partition boots either way.

## 2. Differencing images (`core/vhd.py`, `vhdx.py`, `vdi.py`, `chain.py`)

A Hyper-V checkpoint (`.avhd`, `.avhdx`) or a VirtualBox snapshot stores only the parts of the
disk that changed since its parent. qemu-img can't read these chains:

- a **differencing VHD** is read as if it were a dynamic VHD without a parent — with **no error**,
  giving a disk that lacks everything the parent holds;
- a **differencing VHDX** is refused ("Invalid argument");
- a **differencing VDI** is refused.

VirtConvert implements the block maps of the three formats itself (read-only):

- **VHD**: a big-endian block allocation table; each allocated block starts with a sector bitmap
  (most significant bit first). In a differencing disk, a set bit means the sector is in this
  file, a clear bit means it comes from the parent.
- **VHDX**: two headers (the valid one with the highest sequence number counts), two region
  tables, a metadata region, and a BAT in chunks of `chunk ratio` payload entries plus one sector
  bitmap entry, where `chunk ratio = 2^23 × logical sector size / block size`. Payload blocks are
  *fully present*, *partially present* (look at the sector bitmap, least significant bit first),
  *zero*, or not present/undefined/unmapped (the parent's data in a differencing disk, zeros
  otherwise). Checksums are CRC-32C.
- **VDI**: a block map of 32-bit entries; `0xFFFFFFFF` means "not allocated" (the parent's data in
  a snapshot) and `0xFFFFFFFE` means "zeros".

A `ChainReader` stacks the files: for each part of the disk it asks the newest file, then its
parent, and so on. A parent smaller than the child (the disk was enlarged after the checkpoint)
reads as zeros past its end.

### Finding the parents

Each differencing image names its parent by an ID and, for VHD and VHDX, by paths:

- VHD: the parent's unique ID, its name, and *parent locators* (relative and absolute Windows
  paths, `W2ru`/`W2ku`);
- VHDX: `parent_linkage` (the parent's *DataWriteGuid*, which changes whenever the parent is
  written) and `relative_path`, `absolute_win32_path`, `volume_path`;
- VDI: only the parent's UUID; VirtualBox keeps the paths in the VM's `.vbox` file.

VirtConvert tries, in order: files the user pointed at, the stored paths (Windows paths are
turned into Linux ones and matched without regard to case, relative to the child), the
`.vbox` file of the VM, and finally every image of the same format next to the child, in its
parent folder and in `Snapshots`. A candidate is only accepted if its ID matches. A file with the
right name but a different ID means the parent was modified after the checkpoint was taken;
merging would give a damaged disk, so VirtConvert stops (the user can override this).

### Feeding the merged disk to qemu-img (`core/nbd.py`)

Rather than writing a temporary copy, VirtConvert starts a small read-only **NBD server** on a
Unix socket in a private temporary folder and gives qemu-img
`{"driver": "raw", "file": {"driver": "nbd", "server": {"type": "unix", "path": …}}}` as the
source. The server implements the fixed newstyle handshake (`NBD_OPT_GO`, `NBD_OPT_INFO`,
`NBD_OPT_EXPORT_NAME`) and `NBD_CMD_READ` with simple replies, and refuses everything else.
Unallocated parts read as zeros without touching the disk, and qemu-img leaves zeros out of
sparse targets. The same server pads a disk with zeros when the target has to be larger.

## 3. Planning (`core/convert.py`)

`make_plan` checks everything that can be checked before writing anything: the source is
readable, its parents are found, qemu-img is new enough and has the needed drivers, the target
format can hold the disk (VHD: 2040 GB, VHDX: 64 TB), the output is not the source or one of its
parents, it doesn't exist (unless replacing is allowed), the folder is writable, and the free
space is probably enough. It also collects notes for the user (UEFI → *Enable EFI*; 4 KB sectors
won't work outside VHDX; stream-optimized VMDK is for OVA/ESXi; a checkpoint is being merged).

Targets and the qemu-img options used:

| Target | Options |
|---|---|
| VDI dynamic / fixed | — / `static=on` |
| VMDK regular / stream-optimized | `subformat=monolithicSparse` / `subformat=streamOptimized` |
| VHD dynamic / fixed | `subformat=dynamic,force_size=on` / `subformat=fixed,force_size=on` |
| VHDX dynamic / fixed | `subformat=dynamic` / `subformat=fixed` |
| Azure | fixed VHD, size rounded up to a whole MiB |

Stream-optimized VMDK files are rounded up to whole 64 KB grains: qemu-img writes a partial last
grain that VirtualBox can't read.

## 4. Converting and checking

`execute` writes `<output>.part`, using `qemu-img convert -p` and turning its progress output
into progress for the app and the command line (cancelling stops the process group). Then:

1. **VHDX fix-up**: qemu-img leaves the *required* flag of the BAT and metadata regions clear,
   which [MS-VHDX] asks to be set; VirtualBox refuses such files. VirtConvert sets the flags and
   recomputes the region table checksums.
2. The file is flushed to disk (`fsync`).
3. If the source (or a parent) changed while it was being read, the copy is thrown away: the VM
   was probably running.
4. **Verify**: `qemu-img compare` reads both disks and compares what the guest would see. A size
   difference is fine when the extra part is zeros (Azure rounding, VMDK grains).
5. Only then is `.part` renamed to the real name. Any failure or cancel removes `.part`.

The source files are only ever opened read-only. The one exception is **Repair**, which the user
starts explicitly for a VHDX whose log still holds changes: it runs `qemu-img check -r all`,
which opens the file for writing and so applies the log, as Hyper-V does when it opens the file.

## 5. Testing

- `tests/imagewriter.py` writes VHD, VHDX and VDI files (base and differencing, with partial
  sector bitmaps, zero blocks, 4 KB sectors, dirty logs, GPT and MBR partition tables) straight
  from the specifications. Sectors a differencing file doesn't own are filled with `0xEE`, so a
  reader that ignores a bitmap fails visibly.
- `tests/test_readers.py` reads them back; `tests/test_convert.py` converts in every direction
  and reads the results back through qemu-img.
- `tests/test_references.py` compares with independent implementations: VirtualBox reads every
  format VirtConvert writes and gives the same bytes; merged VHD and VDI chains equal VirtualBox's
  reading of them, including a differencing VHD written by VirtualBox itself; merged VHDX chains
  equal libvhdi's.
- `tests/test_gui.py` drives the app offscreen; `tools/installer-smoke.sh` installs, uses,
  updates and removes VirtConvert inside containers of other distributions (CI).
