# How VirtConvert&Create works

VirtConvert&Create is a careful front end to `qemu-img`: it decides what the source is, finds everything
the source depends on, decides how to write the target, lets qemu-img do the copying, and checks
the result. New disks are put together the same way, from a partition table and a file system
made by the distribution's mkfs program, and a virtual disk is written onto a physical one the
same way too. This page describes each step and why it is there.

## 1. Recognising the image (`core/probe.py`)

qemu-img can guess formats, but it guesses from the first bytes of a file only. A **fixed VHD**
is raw data followed by a 512-byte footer, so qemu-img takes it for a raw image (and would then
copy the footer into the disk). VirtConvert&Create therefore looks for:

| Format | Signature |
|---|---|
| VHDX | `vhdxfile` at offset 0 |
| VHD (dynamic, differencing) | `conectix` at offset 0 (copy of the footer) |
| VHD (fixed) | `conectix` in the last 512 (or 511, very old files) bytes |
| VDI | `0xBEDA107F` at offset 0x40 |
| VMDK | `KDMV` (sparse), `COWD` (ESX), or a `# Disk DescriptorFile` text descriptor |
| QCOW2 / QCOW | `QFI\xfb`, then version 2–3 / 1 |
| QED | `QED\0` |
| Parallels | `WithoutFreeSpace` or `WithouFreSpacExt` |
| DMG (UDIF) | `koly` in the last 512 bytes |
| OVA | `ustar` at offset 257 (a tar archive) |
| OVF | an `<Envelope` XML document in the OVF namespace |
| ISO | `CD001` at offset 0x8001 |
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
off VHDs made by VirtualBox — where a GPT disk keeps its backup partition table. VirtConvert&Create always
uses the byte size and tells qemu-img to do the same (`force_size_calc=current_size`). VHD files
VirtConvert&Create writes are created with `force_size=on`, so their size is exact (Hyper-V and Azure
need that).

### Partition table and boot mode (`core/partitions.py`)

The first 128 KB of the virtual disk are read (by VirtConvert&Create's own readers, or `qemu-img dd`)
and checked for a GPT (at 512-byte and 4 KB sectors) or an MBR. A GPT disk with an EFI system
partition boots with UEFI (VirtualBox: *Enable EFI*; Hyper-V: generation 2, which needs VHDX), an
MBR disk with boot code or an active partition with BIOS (generation 1). A GPT disk with a *BIOS
boot* partition boots either way. Disks with neither (a GPT data disk, an MBR without boot code)
are data disks and get no boot advice.

### Appliances (`core/ova.py`)

An OVA is a tar archive: the OVF descriptor (XML), the disk files, and a manifest of checksums.
Tar stores files uncompressed and in one piece, so a disk inside it is read in place: its offset
and length in the archive go to qemu-img as
`{"driver": "vmdk", "file": {"driver": "raw", "offset": …, "size": …, "file": {"driver": "file", …}}}`.
The descriptor's `References` and `DiskSection` say which members are disks (without a usable
descriptor, members with disk extensions are taken). An OVA with one disk is read as that disk; one
with several becomes one item per disk, named `<archive> › <disk>`. A disk the descriptor marks
as compressed (`ovf:compression="gzip"`) has to be unpacked first. An `.ovf` file stands for the
disk files it lists next to it, and a Parallels `.hdd` bundle for the `.hds` image inside it.

## 2. Differencing images (`core/vhd.py`, `vhdx.py`, `vdi.py`, `chain.py`)

A Hyper-V checkpoint (`.avhd`, `.avhdx`) or a VirtualBox snapshot stores only the parts of the
disk that changed since its parent. qemu-img can't read these chains:

- a **differencing VHD** is read as if it were a dynamic VHD without a parent — with **no error**,
  giving a disk that lacks everything the parent holds;
- a **differencing VHDX** is refused ("Invalid argument");
- a **differencing VDI** is refused.

VirtConvert&Create implements the block maps of the three formats itself (read-only):

- **VHD**: a big-endian block allocation table; each allocated block starts with a sector bitmap
  (most significant bit first). In a differencing disk, a set bit means the sector is in this
  file, a clear bit means it comes from the parent.
- **VHDX**: two headers (the valid one with the highest sequence number counts), two region
  tables, a metadata region, and a BAT in chunks of `chunk ratio` payload entries plus one sector
  bitmap entry, where `chunk ratio = 2^23 × logical sector size / block size`. Payload blocks are
  *fully present*, *partially present* (look at the sector bitmap, least significant bit first),
  *zero*, or not present/undefined/unmapped (the parent's data in a differencing disk, zeros
  otherwise). Checksums are CRC-32C.
Before reading, every block table entry is checked against the file size, like qemu-img does: a
file cut short by an interrupted copy is refused instead of being read as zeros (a copy made from
zeros would still "verify").

VHDX files with 4 KB logical sectors are always read this way, because qemu-img's VHDX driver
only handles 512-byte sectors. The copy gets 512-byte sectors (VDI, VMDK and VHD have no other
kind), which the app warns about: partitions laid out in 4 KB sectors aren't found there.

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

VirtConvert&Create tries, in order: files the user pointed at, the stored paths (Windows paths are
turned into Linux ones and matched without regard to case, relative to the child), the
`.vbox` file of the VM, and finally every image of the same format next to the child, in its
parent folder and in `Snapshots`. A candidate is only accepted if its ID matches. A file with the
right name but a different ID means the parent was modified after the checkpoint was taken;
merging would give a damaged disk, so VirtConvert&Create stops (the user can override this).

### Feeding the merged disk to qemu-img (`core/nbd.py`)

Rather than writing a temporary copy, VirtConvert&Create starts a small read-only **NBD server** on a
Unix socket in a private temporary folder and gives qemu-img
`{"driver": "raw", "file": {"driver": "nbd", "server": {"type": "unix", "path": …}}}` as the
source. The server implements the fixed newstyle handshake (`NBD_OPT_GO`, `NBD_OPT_INFO`,
`NBD_OPT_EXPORT_NAME`) and `NBD_CMD_READ` with simple replies, and refuses everything else.
Unallocated parts read as zeros without touching the disk, and qemu-img leaves zeros out of
sparse targets. The same server pads a disk with zeros when the target has to be larger. For
writing onto a physical disk (section 7) it can also be writable: `NBD_CMD_WRITE`,
`NBD_CMD_WRITE_ZEROES` and `NBD_CMD_FLUSH`.

## 3. Planning (`core/convert.py`)

`make_plan` checks everything that can be checked before writing anything: the source is
readable, its parents are found, qemu-img is new enough and has the needed drivers, the target
format can hold the disk (VHD: 2040 GB, VHDX: 64 TB), the output is not the source or one of its
parents, it doesn't exist (unless replacing is allowed), the folder is writable, and the free
space is probably enough. It also collects notes for the user (UEFI → *Enable EFI*; 4 KB sectors;
stream-optimized VMDK is for OVA/ESXi; a checkpoint is being merged; a file will be replaced).

Targets and the qemu-img options used:

| Target | Options |
|---|---|
| VDI dynamic / fixed | — / `static=on` |
| VMDK dynamic / preallocated | `subformat=monolithicSparse` / `subformat=monolithicFlat` (descriptor + `-flat.vmdk`) |
| VMDK in 2 GB files | `subformat=twoGbMaxExtentSparse` (`-s001.vmdk`…) / `twoGbMaxExtentFlat` (`-f001.vmdk`…) |
| VMDK stream-optimized | `subformat=streamOptimized` |
| VHD dynamic / fixed | `subformat=dynamic,force_size=on` / `subformat=fixed,force_size=on` |
| VHDX dynamic / fixed | `subformat=dynamic` / `subformat=fixed` |
| QCOW2, raw: dynamic / preallocated | — / `preallocation=falloc` |
| QED | — |
| OVA | a stream-optimized VMDK, packed (see below) |
| Azure | fixed VHD, size rounded up to a whole MiB |

A VMDK made of several files is planned and published as a set: every file name is checked
beforehand, and none of them may already exist unless replacing is allowed.

Stream-optimized VMDK files are rounded up to whole 64 KB grains: qemu-img writes a partial last
grain that VirtualBox can't read.

## 4. Converting and checking

`execute` makes a work folder next to the output (`<output>.<random>.part/`) and `write_image`
writes the copy inside it under its final name (a VMDK names its data files in its descriptor),
using `qemu-img convert -p` and turning its progress output into progress for the app and the
command line (cancelling stops the process group). Two runs never share a work folder. Then:

1. **VHDX fix-up**: qemu-img leaves the *required* flag of the BAT and metadata regions clear,
   which [MS-VHDX] asks to be set; VirtualBox refuses such files. VirtConvert&Create sets the flags and
   recomputes the region table checksums.
   **OVA packing**: the stream-optimized VMDK goes into a tar archive after an OVF 1.0 descriptor
   (2 CPUs; 2 GB of memory, 4 GB when the partitions look like Windows; a SATA controller with the
   disk; a NAT network card; VirtualBox's guest type, and VMware's `firmware=efi` for UEFI disks)
   and before a manifest with SHA-256 checksums. Archives use ustar, or GNU tar's binary size
   field for disks over 8 GB.
2. The file is flushed to disk (`fsync`).
3. If the source (or a parent) changed while it was being read, the copy is thrown away: the VM
   was probably running.
4. **Verify**: `qemu-img compare` reads both disks and compares what the guest would see (for an
   OVA, the disk inside the new archive). A size difference is fine when the extra part is zeros
   (Azure rounding, VMDK grains).
5. Only then do the files get their real names: with hard links, which fail if a file of that
   name appeared in the meantime (nothing is replaced unless the user allowed it; the files already
   placed are taken back, and the checked copy is kept and its location reported). A VMDK's data
   files come first and its descriptor last, so the disk appears only when it is complete. Any
   failure or cancel removes the work folder.

The source files are only ever opened read-only. The one exception is **Repair**, which the user
starts explicitly for a VHDX whose log still holds changes: it runs `qemu-img check -r all`,
which opens the file for writing and so applies the log, as Hyper-V does when it opens the file.

## 5. New disks (`core/create.py`)

`make_create_plan` checks the size (rounded up to whole MB), the format's limit, the partition
table (MBR reaches 2 TB), the file system's minimum and maximum (FAT32: 33 MB–2 TB, Btrfs from
109 MB, XFS from 300 MB), that its mkfs program is installed (in `PATH` or the `sbin` folders,
which aren't in a normal user's `PATH` on Debian), and the output like a conversion does.

An **empty** disk is made by `qemu-img create` directly, and checked by opening it. A
**partitioned** or **formatted** one is put together in a scratch folder in the work folder (or
the temporary folder, when the output drive has no sparse files, like FAT and exFAT):

1. a sparse raw file of the disk's size gets the partition table: a GPT (protective MBR, header,
   128 entries and their backup at the end, CRC-32 checksums) or an MBR, with one partition from
   1 MB to the last whole MB (before the backup GPT). The partition type is Microsoft's *basic
   data* for NTFS, exFAT and FAT32, *Linux file system* for the others (MBR: `0x07`, `0x0C`,
   `0x83`), and the GPT partition is named after the label;
2. the mkfs program formats a second sparse file of the partition's size. NTFS and FAT32 are
   told where the partition starts (`mkntfs -p`, `mkfs.fat -h`), as Windows expects. ext4 is made
   with lazy inode table and journal initialisation (the file is zeros anyway) and
   `packed_meta_blocks`, which keeps its metadata together at the start instead of spread over the disk;
3. the parts of that file holding data (found with `SEEK_DATA`/`SEEK_HOLE`, zero runs left out)
   are copied into the disk file at the partition's offset;
4. `write_image` writes the target from the raw file like a conversion; qemu-img skips the holes,
   so a 2 TB formatted disk takes a second or two. A dynamic VHDX with a Linux file system gets
   1 MB blocks (`block_size`; Microsoft's advice for Linux guests), since Linux file systems spread
   their metadata over the disk and each touched 32 MB block would otherwise be allocated.

The new disk is compared with the raw file: by `qemu-img compare`, or for a VHDX (dynamic or fixed)
by VirtConvert&Create's own reader, which only reads what either side holds: the raw file's data,
and the VHDX blocks whose bytes in the file aren't holes. (qemu-img's VHDX driver can't report empty
blocks, so its compare would read every byte of a large empty disk: over an hour for a few TB.)

## 6. Physical disks (`core/devices.py`)

A computer's disks (and USB sticks, memory cards) can be the source of a conversion, or of a new
disk ("Copy of a physical disk"). They come from `lsblk -J -b` (string or typed values, both kinds
of util-linux), without zram, RAM disks, loop devices, empty card readers and eMMC boot areas;
`lsblk` also gives the model, bus, partitions, file systems, labels and where they are mounted, so
nothing has to be read before converting. When the disk is readable without a password (root, the
`disk` group), its partition table is read directly instead, which also works without udev.

Reading a disk needs root. VirtConvert&Create never runs qemu-img or itself as root for that: a
few lines of Python started with `pkexec` (the app: a password dialog) or `sudo` (the command line)
check that the path is a block device, open it read-only, and send the open file descriptor back
over a Unix socket in a private folder (`SCM_RIGHTS`). The descriptor is checked to be that device,
and the disk is served to qemu-img by the NBD server like a merged chain (`DeviceReader`: zeros past
the end when the target is padded, a clear message for unreadable sectors). The copy runs as the
user, can be cancelled, and the files belong to them.

`make_plan` refuses to save the image on the disk being copied (the folder's device, through
`/proc/self/mountinfo` for btrfs and the like, and LVM/LUKS layers down to the disks, is compared
with the source), and when the disk is mounted it warns (more strongly for the system disk) and
skips the comparison afterwards: a disk in use keeps changing, so it would fail for no fault of the
copy.

### Some partitions

The copy can also be of some partitions only (`Job.partitions`; a partition's own path such as
`/dev/sdb2` stands for its disk with that partition). `plan_partition_copy` places them in a new
disk, one after the other from 1 MiB on, each starting on a whole MiB, in the original order; the
new disk ends 1 MiB after the last one (the room a GPT backup needs). Where they are on the
physical disk comes from `/sys/class/block/*/start` (always in 512-byte units) and their numbers
from `.../partition`, so planning needs no password; the kind of table comes from lsblk, or from
the disk's first sectors when lsblk has no udev data and the disk is readable, or else is settled
once the disk is open.

`PartitionReader` makes the new disk on the fly for the NBD server. Once the disk is open it reads
the disk's own table with `partitions.read_entries` (GPT entries; MBR primaries, and the logical
partitions through the extended partition's chain of EBRs, whose links are relative to the
extended partition and whose partitions to their EBR), checks that every chosen partition is still
where the plan put it, and builds the new table from those real entries: a GPT keeps each entry's
type, unique GUID (so `PARTUUID=` references still match), attributes and name, with only its first
and last LBA rewritten (`gpt_with_entries`, a new disk GUID); an MBR keeps each type and the active
flag (`mbr_table`, a new signature). Reads of the new disk are served from the table, from the
partitions' bytes where they are on the disk, and as zeros in between. Only the chosen partitions
are ever read, and nothing is written.

`_device_checks` is partition-aware for such a copy: only the chosen partitions' mountpoints count
as in use, and the image may be saved on another partition of the same disk, never on one being
copied (`partitions_holding`, which follows LVM and LUKS down to partitions instead of disks).

## 7. Writing onto a physical disk (`core/writedisk.py`)

Any disk VirtConvert&Create reads can be written onto one of the computer's disks: every format, a
merged chain, a disk inside an OVA, another physical disk.

`make_write_plan` refuses what would go wrong or destroy something else: a partition instead of a
whole disk, an empty card reader, a write-protected disk, the disk the running system uses (`/`,
`/boot`, `/home`, swap…), a disk with LVM, RAID or unlocked encrypted volumes on it, the disk the
image (or any file of its chain) is stored on, and a disk whose sector size doesn't match the
image's partition table (a 512-byte GPT is not found on a 4 KB-sector disk; an MBR counts in the
disk's own sectors too). The app passes the size, model, serial number and `diskseq` (the kernel's
number for every disk and medium it sees, so two cards in one reader differ) of the disk the
person saw. The plan is made again right before asking, again before writing, and once more after
the disk is open (the password dialogs are over and the disk is held): a different stick plugged
in under the same name is refused. A loop device whose backing file is the image, and network
block devices (which could be anything), are refused too. Mounted file systems
of the disk are listed to be unmounted first (`udisksctl unmount`, as the file manager does; then
`umount`, as root or through `sudo`/`pkexec`).

The disk is opened by the same root helper as for reading, now with `O_RDWR | O_EXCL`: the kernel
refuses the exclusive open while anything on the disk is mounted or held by device-mapper or md,
and nothing can mount it while it is open. qemu-img then runs `convert -n -O raw` into the
writable NBD server, as the user, which writes into the disk (`DeviceWriter`). Zeros are written
too (as `BLKZEROOUT`, which lets the disk zero itself when it can), so nothing of the old contents
shows through. Every 64 MB the written part is pushed out of the page cache (`POSIX_FADV_DONTNEED`
starts the write-back), so a slow USB stick doesn't collect gigabytes in memory that take minutes
to reach it at the end; then `fsync`. Comparing drops the disk's cache first, so it reads what is
on the disk, not what the computer remembers.

The size: an image no larger than the disk is written whole. One larger than the disk is accepted
when its partitions (with a GPT's backup, as large as its own entry array, after them) end before
the disk's end; then the first part is written, through a raw `size` limit around the source's
qemu-img specification. Where the partitions are has to be known for that: a physical disk read
without a password only has lsblk's sizes, so the starts come from `/sys/class/block/*/start`, and
right before writing the source's partition table is read once more through the very reader that
is about to be copied. Afterwards
the disk is fitted (`move_backup_gpt`, what `sgdisk -e` does): the backup GPT entries and header go
to the disk's last sectors, the last usable sector and the protective MBR follow the disk's size,
and the image's old backup header, now in the middle of a larger disk, is cleared. Linux ignores a
GPT whose partitions may lie past the disk's end, and partitioning tools complain about a backup
in the wrong place. The last megabyte of a larger disk is cleared too: old GPT backups, RAID
superblocks and ZFS labels live there and would confuse `blkid`. If a GPT can't be fitted to a
smaller disk, that is an error, not a note: the disk wouldn't work.

## 8. Testing

- `tests/imagewriter.py` writes VHD, VHDX, VDI and DMG files (base and differencing, with partial
  sector bitmaps, zero blocks, 4 KB sectors, dirty logs, GPT and MBR partition tables) straight
  from the specifications. Sectors a differencing file doesn't own are filled with `0xEE`, so a
  reader that ignores a bitmap fails visibly.
- `tests/test_readers.py` reads them back; `tests/test_convert.py` converts in every direction
  and reads the results back through qemu-img; `tests/test_formats.py` covers OVA and OVF
  (several disks, no descriptor, compressed or missing disks), QED, QCOW, Parallels, DMG and ISO.
- `tests/test_create.py` makes every file system with GPT and MBR and checks it with its own
  `fsck`, `blkid` and `sgdisk`, plus the GPT checksums and layout against the specification.
- `tests/test_references.py` compares with independent implementations: VirtualBox reads every
  format VirtConvert&Create writes and gives the same bytes, imports its OVA files, reads its new
  disks, and exports an OVA that VirtConvert&Create reads; merged VHD and VDI chains equal
  VirtualBox's reading of them, including a differencing VHD written by VirtualBox itself; merged
  VHDX chains equal libvhdi's.
- `tests/test_devices.py` parses lsblk output of both kinds, checks the helper that hands over the
  disk, and copies a loop device (as root, or with passwordless sudo, which also runs the real
  helper).
- `tests/test_writedisk.py` writes every format, a chain and an OVA onto regular files standing in
  for disks (larger, smaller, mounted, failing), checks the refusals and the fitted GPT with
  `sfdisk --verify`, and writes onto loop devices: exclusively opened, mounted ones unmounted,
  disk to disk, and write-protected ones refused.
- `tests/test_gui.py` drives the app offscreen; `tools/installer-smoke.sh` installs, uses,
  updates and removes VirtConvert&Create inside containers of other distributions (CI).
