# Maintainer: VirtConvert&Create contributors
#
# Arch Linux and Arch-based distributions (CachyOS, EndeavourOS, Manjaro, Garuda, ...).
# Builds the checkout this file sits in:
#
#   git clone https://github.com/oreo1298/VirtConvert.git
#   cd VirtConvert
#   makepkg -si
#
# Update:     git pull && makepkg -sif
# Uninstall:  sudo pacman -Rns virtconvert

pkgname=virtconvert
pkgver=1.1.0
pkgrel=1
pkgdesc="Convert and create virtual disks: VDI, VMDK, VHD, VHDX, QCOW2, OVA and more, checkpoints included"
arch=('any')
url="https://github.com/oreo1298/VirtConvert"
license=('MIT')
depends=('python' 'pyside6' 'qt6-svg' 'qt6-wayland' 'qemu-img')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
checkdepends=('python-pytest')
optdepends=('virtualbox: run the converted disks'
            'polkit: copy physical disks from the app (password dialog)'
            'dosfstools: new disks formatted with FAT32'
            'exfatprogs: new disks formatted with exFAT'
            'ntfsprogs: new disks formatted with NTFS'
            'btrfs-progs: new disks formatted with Btrfs'
            'xfsprogs: new disks formatted with XFS')
# vertconvert: the 1.1 build that was briefly misspelt VertConvert&Create
conflicts=('vertconvert')
replaces=('vertconvert')
install=virtconvert.install
source=()
sha256sums=()

# Find the checkout from wherever makepkg was started.
_srcroot() {
  local dir="$startdir"
  while [[ ! -f "$dir/pyproject.toml" && "$dir" != / ]]; do
    dir="$(dirname "$dir")"
  done
  if [[ ! -f "$dir/pyproject.toml" ]]; then
    echo "Run makepkg inside the VirtConvert folder." >&2
    return 1
  fi
  printf '%s\n' "$dir"
}

prepare() {
  local root
  root="$(_srcroot)" || return 1
  rm -rf "$srcdir/$pkgname"
  mkdir -p "$srcdir/$pkgname"
  # Copy the checkout, leaving out git data and makepkg's own work folders.
  tar -C "$root" \
      --exclude=./.git --exclude=./src --exclude=./pkg \
      --exclude='*.pkg.tar*' --exclude='__pycache__' --exclude='*.egg-info' \
      --exclude=./build --exclude=./dist --exclude=./.venv \
      -cf - . | tar -C "$srcdir/$pkgname" -xf -
}

build() {
  cd "$srcdir/$pkgname"
  python -m build --wheel --no-isolation
}

check() {
  cd "$srcdir/$pkgname"
  PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider -m "not gui and not vbox and not libvhdi" tests
}

package() {
  cd "$srcdir/$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 data/io.github.oreo1298.VirtConvert.desktop \
    "$pkgdir/usr/share/applications/io.github.oreo1298.VirtConvert.desktop"
  install -Dm644 data/io.github.oreo1298.VirtConvert.metainfo.xml \
    "$pkgdir/usr/share/metainfo/io.github.oreo1298.VirtConvert.metainfo.xml"
  install -Dm644 virtconvert/data/virtconvert.svg \
    "$pkgdir/usr/share/icons/hicolor/scalable/apps/io.github.oreo1298.VirtConvert.svg"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 NOTICE "$pkgdir/usr/share/licenses/$pkgname/NOTICE"
  install -Dm644 README.md docs/HOW-IT-WORKS.md -t "$pkgdir/usr/share/doc/$pkgname/"
}
