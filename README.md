# VirtConvert: Hyper-V disks for VirtualBox, and back

VirtConvert converts **Hyper-V disk images (VHD, VHDX)** into formats **VirtualBox** uses (VDI,
VMDK), and VirtualBox disks back into **VHD or VHDX** for Hyper-V and Azure. It is made for Arch
Linux and installs just as easily on Debian, Ubuntu, Fedora, openSUSE and the other popular
distributions. There is a desktop app in the style of [EZP2019Linux](https://github.com/oreo1298/EZP2019Linux)
and a command line for scripts.

![Main window, dark theme](docs/screenshots/main-dark.png)

## Features

- **Hyper-V → VirtualBox**: VHD and VHDX become VDI (VirtualBox's own format) or VMDK.
- **VirtualBox → Hyper-V**: VDI and VMDK become VHDX (Hyper-V generation 1 and 2) or VHD
  (generation 1, Virtual PC, Azure).
- **Checkpoints and snapshots**: a Hyper-V checkpoint (`.avhdx`, `.avhd`) or a VirtualBox
  snapshot only holds what changed since its parent. VirtConvert finds the parents (also when the
  files were moved or renamed) and merges the whole chain into one complete disk. The original
  files are never changed.
- **Every copy is checked**: after converting, the new disk is compared with the source byte for
  byte before it gets its real name. An interrupted or failed conversion leaves nothing behind.
- **Tells you how to boot it**: VirtConvert reads the partition table and says whether the guest
  boots with UEFI or BIOS, so you know to turn on *Enable EFI* in VirtualBox, or which Hyper-V
  generation to pick.
- **Options for each target**: dynamic or fixed disks, stream-optimized VMDK for OVA appliances
  and VMware ESXi, and Azure-ready VHD files (fixed, rounded up to whole megabytes).
- **Several images at once**: a list you can fill by drag and drop, converted one after the other
  with progress, speed and time left, and **Cancel**.
- **Command line**: `virtconvert info`, `convert`, `verify`, `repair`, `formats` and `doctor`.
- Dark and light themes that follow the desktop setting.

VirtConvert also fixes three problems you would hit with `qemu-img` alone:

| Problem with plain `qemu-img` | What VirtConvert does |
|---|---|
| A differencing VHD is converted **without its parent, without any error**: the result is missing most of the disk. A differencing VHDX is refused. | Reads the chain itself and hands qemu-img the merged disk. |
| A fixed VHD is taken for a raw image, and VirtualBox-made VHDs are cut short by a few KB (qemu-img sizes them by their CHS geometry). | Detects the format itself and always uses the full disk size, like Hyper-V and VirtualBox do. |
| VHDX files written by qemu-img can't even be read by VirtualBox (the "required" flag on their block table is missing). | Sets the flag the VHDX specification asks for, so VirtualBox can read them too. |

| Going back to Hyper-V (light theme) | Converting |
|---|---|
| ![Back to Hyper-V](docs/screenshots/back-light.png) | ![Progress](docs/screenshots/progress-dark.png) |

## Installation

VirtConvert needs **Python 3.10+**, **qemu-img** (from the QEMU project; it writes the converted
disks) and, for the app, **PySide6** (Qt 6). The installers below take care of all of it.
VirtualBox itself is not needed for converting.

### Arch Linux, CachyOS, EndeavourOS, Manjaro, Garuda and other Arch-based distributions

The repository includes a PKGBUILD, so VirtConvert installs as a normal pacman package. Run these
as your normal user (not root); `makepkg` asks for your password when it installs the package:

```sh
sudo pacman -S --needed git base-devel
git clone https://github.com/oreo1298/VirtConvert.git
cd VirtConvert
makepkg -si
```

This installs the `virtconvert` command, **VirtConvert** in your application menu, and the
dependencies `python`, `pyside6`, `qt6-svg`, `qt6-wayland` and `qemu-img`.

- **Update:** pull the new code and rebuild from the same folder:

  ```sh
  cd VirtConvert
  git pull
  makepkg -sif
  ```

  (`-f` rebuilds even if a package of the same version is already there.)
- **Uninstall:** `sudo pacman -Rns virtconvert` (your settings in `~/.config/virtconvert` stay;
  delete the folder if you like. qemu-img is removed too if nothing else needs it.)

If `makepkg` says `PKGBUILD does not exist`, you are not inside the `VirtConvert` folder.

### Every other distribution: the installer

`packaging/install.sh` recognises your package manager (**apt, dnf, zypper, xbps, apk, eopkg,
emerge**), installs qemu-img and Python after asking, installs VirtConvert into
`/opt/virtconvert`, and adds the `virtconvert` command and the menu entry:

```sh
git clone https://github.com/oreo1298/VirtConvert.git
cd VirtConvert
./packaging/install.sh          # asks for your password (sudo)
```

First install git for your distribution:

| Distribution | Install git first |
|---|---|
| **Ubuntu 22.04 and newer, Linux Mint, Pop!_OS, Zorin OS, elementary OS, KDE neon, Debian 12/13** | `sudo apt install git` |
| **Fedora, Nobara, Ultramarine** | `sudo dnf install git` |
| **RHEL, AlmaLinux, Rocky Linux 9+** | `sudo dnf install git` |
| **openSUSE Tumbleweed / Leap** | `sudo zypper install git` |
| **Void Linux** | `sudo xbps-install -S git bash` |
| **Alpine Linux** | `sudo apk add git bash` (enable the community repository for PySide6) |
| **Solus** | `sudo eopkg install git` |
| **Gentoo** | `sudo emerge --noreplace dev-vcs/git` |

- **Update:** `cd VirtConvert && ./packaging/update.sh` (pulls the latest version and reinstalls
  it the same way; your settings stay). `make update` does the same.
- **Uninstall:** `./packaging/uninstall.sh` (add `--purge` to also delete your settings; your
  disk images are never touched, and qemu-img stays installed). `make uninstall` does the same.

Notes:

- Where your distribution packages PySide6 (Fedora, openSUSE Tumbleweed, Debian 13, Alpine), it is
  used; elsewhere (Ubuntu 22.04/24.04, Mint 21/22, Debian 12, RHEL/Rocky) the installer downloads
  PySide6-Essentials (about 100 MB) into VirtConvert's own folder. Nothing else on your system
  changes.
- RHEL/Rocky 9 and openSUSE Leap ship an older Python next to newer ones; the installer picks or
  installs Python 3.10+ by itself.
- If a package doesn't exist on your release, the installer says so and installs the rest;
  `virtconvert doctor` shows what's still missing and the command to install it.
- **Immutable systems** (Fedora Silverblue/Kinoite, Bazzite, SteamOS, openSUSE Aeon): run
  `sudo rpm-ostree install qemu-img` (then restart), and use the per-user install below.
- **NixOS**: add `qemu-utils` and `python3Packages.pyside6` to your configuration, then run
  `./virtconvert.sh` or the per-user install.

### Per-user install (no sudo for VirtConvert itself)

```sh
./packaging/install.sh --user        # into ~/.local; qemu-img still comes from your package manager
./packaging/update.sh                # update
./packaging/uninstall.sh --user      # remove
```

Make sure `~/.local/bin` is on your `PATH` (the installer tells you if it isn't).

### Try it without installing

```sh
./virtconvert.sh                     # the app (needs Python, PySide6 and qemu-img)
./virtconvert.sh convert disk.vhdx   # the command line
```

### Check the installation

```sh
virtconvert doctor
```

```
VirtConvert 1.0.0 on Ubuntu 24.04.5 LTS
  ✓ Python             3.12.3
  ✓ qemu-img           8.2.2 (/usr/bin/qemu-img)
  ✓ PySide6 (the app)  6.11.2
  · VirtualBox         not installed
    Not needed for converting; install it to run the converted disks.
```

If qemu-img is missing, `virtconvert doctor` prints the command for your distribution, and
`virtconvert doctor --install-qemu` (or **Help → Install qemu-img…** in the app) installs it.

## Using the app

1. Start **VirtConvert** from your application menu (or run `virtconvert`).
2. **Add** your images, or drop them on the window. Hyper-V disks are set to become VDI files for
   VirtualBox, VirtualBox and VMware disks to become VHDX files for Hyper-V. For a checkpoint
   (`.avhdx`), add the newest one: it is merged with its parents.
3. Select an image to see what it holds (size, partitions, how it boots, its parents) and to
   change the target under **Convert to** and the file name under **Save as**. The box under
   it lists anything worth knowing before converting.
4. Click **Convert** (<kbd>F5</kbd>). The converted file is saved next to the original unless you
   pick a folder under **Options**. Double-click a finished image to open its folder.

| Action | Shortcut |
|---|---|
| Add images | <kbd>Ctrl</kbd>+<kbd>O</kbd> |
| Convert | <kbd>F5</kbd> |
| Cancel | <kbd>Esc</kbd> |
| Remove from the list | <kbd>Del</kbd> |
| Compare two images | <kbd>Ctrl</kbd>+<kbd>K</kbd> |
| Open the folder of the converted file | <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>O</kbd> |
| Toggle dark/light theme | <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>T</kbd> |

### Using a converted disk in VirtualBox

1. Shut the virtual machine down in Hyper-V first (not *Save*): a running or saved VM gives an
   inconsistent copy (VirtConvert stops if a file changes while it is being converted).
2. In VirtualBox: **New**, choose the guest type, and under **Hard Disk** pick **Use an Existing
   Virtual Hard Disk File** and select the `.vdi` (or `.vmdk`).
3. If VirtConvert said the disk **boots with UEFI** (Hyper-V generation 2 VMs, most Windows 10/11
   installs), turn on **Settings → System → Enable EFI**.
4. Windows guests: if Windows stops with `INACCESSIBLE_BOOT_DEVICE`, attach the disk to a SATA
   (AHCI) controller (the default). Install the VirtualBox Guest Additions afterwards; you can
   uninstall the Hyper-V integration services.

### Using a converted disk in Hyper-V

1. In Hyper-V Manager: **New → Virtual Machine**. Pick **Generation 2** if VirtConvert said the
   disk boots with **UEFI**, **Generation 1** for **BIOS**. (Generation 2 needs VHDX.)
2. Under **Connect Virtual Hard Disk**, choose **Use an existing virtual hard disk** and select the
   `.vhdx` or `.vhd` file.
3. Linux guests on generation 2: turn Secure Boot off or use the *Microsoft UEFI Certificate
   Authority* template.

For **Azure**, convert with **VHD** and *For Azure* ticked (`virtconvert convert -t azure`): Azure
needs a fixed VHD whose size is a whole number of megabytes. Upload it as a page blob.

## Command line

Running `virtconvert` without a command opens the app (`virtconvert disk.vhdx` opens it with the
file in the list). Everything is also available as a command:

```sh
virtconvert info disk.vhdx                  # format, size, partitions, boot mode, parents
virtconvert info --json *.vhdx              # the same, for scripts

virtconvert convert disk.vhdx               # -> disk.vdi (VHD/VHDX become VDI)
virtconvert convert disk.vdi                # -> disk.vhdx (everything else becomes VHDX)
virtconvert convert "Win_8E2C….avhdx"       # a checkpoint: merged into one disk -> Win.vdi
virtconvert convert -t vmdk -d ~/VMs *.vhdx # several at once, into a folder
virtconvert convert -t vhdx --fixed disk.vdi
virtconvert convert -t azure -o os.vhd disk.vdi
virtconvert convert -t ova disk.vhdx        # stream-optimized VMDK for an OVA appliance

virtconvert verify disk.vhdx disk.vdi       # do two images contain the same disk?
virtconvert repair disk.vhdx                # apply the log of a VHDX that wasn't closed cleanly
virtconvert formats                         # formats and shortcuts
virtconvert doctor                          # is everything installed?
```

Useful options for `convert`: `-t/--to` (`vdi`, `vmdk`, `vhd`, `vhdx`, `qcow2`, `raw`, or the
shortcuts `virtualbox`, `hyperv`, `vmware`, `ova`, `azure`), `-o FILE`, `-d FOLDER`, `--fixed`,
`--overwrite`, `--no-verify`, `--parent FILE` (where a checkpoint's parent is, if it isn't found)
and `-y` (don't ask about warnings). Exit status: 0 done, 1 failed, 130 cancelled.

## Formats

| Format | Read | Write | Opens in |
|---|---|---|---|
| **VDI** (`.vdi`) — dynamic, fixed, snapshots | ✓ | ✓ | VirtualBox |
| **VMDK** (`.vmdk`) — sparse, stream-optimized, split, flat, snapshots | ✓ | ✓ (sparse, stream-optimized) | VirtualBox, VMware |
| **VHD** (`.vhd`, `.avhd`) — fixed, dynamic, differencing | ✓ | ✓ (fixed, dynamic) | Hyper-V gen. 1, Virtual PC, Azure, VirtualBox |
| **VHDX** (`.vhdx`, `.avhdx`) — fixed, dynamic, differencing, 512 B and 4 KB sectors | ✓ | ✓ (fixed, dynamic) | Hyper-V gen. 1 and 2 (VirtualBox only reads VHDX: convert to VDI to run it) |
| **QCOW2** (`.qcow2`) | ✓ | ✓ | QEMU/KVM, Proxmox, virt-manager |
| **Raw** (`.img`, `.raw`) | ✓ | ✓ | dd, everything |

Limits: VHD files hold up to 2040 GB, VHDX files up to 64 TB.

## Troubleshooting

- **"qemu-img missing"**: run `virtconvert doctor` for the install command, or click the
  qemu-img box in the toolbar.
- **"The parent of … was not found"**: a checkpoint or snapshot needs the files it is based on.
  Put them in the same folder (Hyper-V keeps them in *Virtual Hard Disks*, VirtualBox in the VM's
  folder and its *Snapshots* folder), or use **Choose parent…** (`--parent` on the command line).
  VirtConvert matches parents by their ID, so renamed files are found too.
- **"has the right name but a different ID"**: the parent was changed after the checkpoint was
  made (for example, it was started on its own). Merging them would give a damaged disk. If you
  are sure the files belong together, **Choose parent…** offers to use it anyway.
- **"The VHDX was not closed cleanly"**: the VM was running or Windows crashed, and the file's log
  still holds changes. Shut the VM down in Hyper-V, or use **Repair…** (`virtconvert repair`),
  which applies the log to the file like Hyper-V would. Make a copy first if you can.
- **"This disk uses 4 KB sectors"**: VirtualBox shows disks with 512-byte sectors, so a guest
  installed on a 4 KB-sector VHDX won't find its partitions there. Keep such disks as VHDX.
- **VirtualBox says the UUID already exists**: every conversion gets a new ID, but a copied file
  keeps its ID. Convert again, or give the copy a new ID with
  `VBoxManage internalcommands sethduuid file.vdi`.
- **Not enough space**: a dynamic target needs about as much space as the data on the disk, a
  fixed one the whole disk size. VirtConvert warns before it starts.
- **Reporting a problem**: include the output of `virtconvert doctor` and `virtconvert info` for
  the image, and the Activity log.

## How it works

VirtConvert reads the image headers itself (VHD, VHDX, VDI, VMDK, QCOW2) instead of letting
qemu-img guess, finds the parents of differencing images and reads them as one disk, then runs
`qemu-img convert` with the format given explicitly. For a checkpoint chain, the merged disk is
served to qemu-img through a small read-only NBD server on a private socket, so no temporary copy
of the disk is needed. Afterwards `qemu-img compare` checks the copy against the source. Details
in [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md).

| Path | Contents |
|---|---|
| `virtconvert/core/probe.py` | Recognising formats, reading headers |
| `virtconvert/core/vhd.py`, `vhdx.py`, `vdi.py` | Block maps of VHD, VHDX and VDI, differencing included |
| `virtconvert/core/chain.py` | Finding parents, reading a chain as one disk |
| `virtconvert/core/nbd.py` | The read-only NBD server qemu-img reads merged chains from |
| `virtconvert/core/qemu.py` | Running qemu-img |
| `virtconvert/core/convert.py` | Planning, converting, verifying |
| `virtconvert/core/partitions.py` | Partition tables and the boot mode |
| `virtconvert/gui/` | PySide6 app |
| `virtconvert/cli.py` | Command line |

The readers are tested against images written from the format specifications, and against
VirtualBox and libvhdi as independent implementations (`tests/test_references.py`): VirtualBox
reads every format VirtConvert writes, and merged checkpoint chains match what VirtualBox and
libvhdi read.

## Development

```sh
python -m venv --system-site-packages .venv && . .venv/bin/activate
pip install -e '.[dev]'
make test                               # 100+ tests; the app's run offscreen
make lint
./virtconvert.sh                        # the app from this folder
make screenshots                        # regenerate docs/screenshots
```

The tests need qemu-img. With VirtualBox and libvhdi's Python module (`python3-libvhdi`)
installed, the reference tests run too.

## Credits and license

MIT licensed, see [LICENSE](LICENSE) and [NOTICE](NOTICE). The conversion is done by qemu-img
from the [QEMU](https://www.qemu.org) project. The look of the app is shared with
[EZP2019Linux](https://github.com/oreo1298/EZP2019Linux) and the other apps by the same author.
VirtConvert is not affiliated with Microsoft, Oracle or VMware.
