"""Host checks: which distribution, which package manager, are qemu-img and the mkfs tools there."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .qemu import QemuImg

# package manager -> (package with qemu-img, install command run as root)
QEMU_PACKAGES: dict[str, tuple[str, list[str]]] = {
    "pacman": ("qemu-img", ["pacman", "-S", "--needed", "--noconfirm", "qemu-img"]),
    "apt-get": ("qemu-utils", ["apt-get", "install", "-y", "qemu-utils"]),
    "dnf": ("qemu-img", ["dnf", "install", "-y", "qemu-img"]),
    "yum": ("qemu-img", ["yum", "install", "-y", "qemu-img"]),
    "zypper": ("qemu-tools", ["zypper", "--non-interactive", "install", "qemu-tools"]),
    "xbps-install": ("qemu-img", ["xbps-install", "-Sy", "qemu-img"]),
    "apk": ("qemu-img", ["apk", "add", "qemu-img"]),
    "eopkg": ("qemu", ["eopkg", "install", "-y", "qemu"]),
    "emerge": ("app-emulation/qemu", ["emerge", "--noreplace", "app-emulation/qemu"]),
}
# the same, as people type it
QEMU_COMMANDS = {
    "pacman": "sudo pacman -S qemu-img",
    "apt-get": "sudo apt install qemu-utils",
    "dnf": "sudo dnf install qemu-img",
    "yum": "sudo yum install qemu-img",
    "zypper": "sudo zypper install qemu-tools",
    "xbps-install": "sudo xbps-install -S qemu-img",
    "apk": "sudo apk add qemu-img",
    "eopkg": "sudo eopkg install qemu",
    "emerge": "sudo emerge --ask app-emulation/qemu",
}
# how people install a package, by package manager
INSTALL_PREFIX = {
    "rpm-ostree": "sudo rpm-ostree install", "pacman": "sudo pacman -S", "apt-get": "sudo apt install",
    "dnf": "sudo dnf install", "yum": "sudo yum install", "zypper": "sudo zypper install",
    "xbps-install": "sudo xbps-install -S", "apk": "sudo apk add", "eopkg": "sudo eopkg install",
    "emerge": "sudo emerge --ask",
}
# the programs that make file systems, and their package by package manager (default: the first)
MKFS_PACKAGES: dict[str, dict[str, str]] = {
    "fat": {"*": "dosfstools", "emerge": "sys-fs/dosfstools"},
    "exfat": {"*": "exfatprogs", "emerge": "sys-fs/exfatprogs"},
    "ntfs": {"*": "ntfs-3g", "pacman": "ntfsprogs", "dnf": "ntfsprogs", "yum": "ntfsprogs", "rpm-ostree": "ntfsprogs",
             "zypper": "ntfsprogs", "apk": "ntfs-3g-progs", "emerge": "sys-fs/ntfs3g"},
    "ext4": {"*": "e2fsprogs", "emerge": "sys-fs/e2fsprogs"},
    "btrfs": {"*": "btrfs-progs", "zypper": "btrfsprogs", "emerge": "sys-fs/btrfs-progs"},
    "xfs": {"*": "xfsprogs", "emerge": "sys-fs/xfsprogs"},
}
# mkfs programs often live in sbin folders that aren't on a normal user's PATH (Debian)
SBIN = ("/usr/local/sbin", "/usr/sbin", "/sbin")
ORDER = ("rpm-ostree", "pacman", "apt-get", "dnf", "yum", "zypper", "xbps-install", "apk", "eopkg",
         "emerge", "nix-env")


def os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if "=" in line:
                        key, _, value = line.strip().partition("=")
                        data[key] = value.strip().strip('"')
            break
        except OSError:
            continue
    return data


def distro_name() -> str:
    rel = os_release()
    return rel.get("PRETTY_NAME") or rel.get("NAME") or "Linux"


def package_manager() -> str | None:
    if Path("/run/ostree-booted").exists() and shutil.which("rpm-ostree"):
        return "rpm-ostree"
    for pm in ORDER:
        if pm != "rpm-ostree" and shutil.which(pm):
            return pm
    return None


def qemu_install_command() -> str:
    """The command to install qemu-img on this system, for people to copy."""
    pm = package_manager()
    if pm == "rpm-ostree":
        return "sudo rpm-ostree install qemu-img   (then restart)"
    if pm == "nix-env" or Path("/etc/NIXOS").exists():
        return "add qemu-utils to environment.systemPackages (or: nix-shell -p qemu-utils)"
    if pm in QEMU_COMMANDS:
        return QEMU_COMMANDS[pm]
    return "install qemu-img (often in a package called qemu-utils, qemu-tools or qemu-img)"


def find_program(*names: str) -> str | None:
    """The first of NAMES found on PATH or in the sbin folders."""
    for name in names:
        found = shutil.which(name) or shutil.which(name, path=os.pathsep.join(SBIN))
        if found:
            return found
    return None


def package_for(tool_key: str) -> str:
    names = MKFS_PACKAGES[tool_key]
    return names.get(package_manager() or "", names["*"])


def install_command(package: str) -> str:
    """The command that installs PACKAGE here, for people to copy."""
    pm = package_manager()
    if pm in INSTALL_PREFIX:
        return f"{INSTALL_PREFIX[pm]} {package}" + ("   (then restart)" if pm == "rpm-ostree" else "")
    return f"install {package} with your package manager"


def install_qemu(graphical: bool = False) -> tuple[bool, str]:
    """Install qemu-img with the package manager, elevating with pkexec or sudo."""
    pm = package_manager()
    if pm not in QEMU_PACKAGES:
        return False, f"Install qemu-img yourself: {qemu_install_command()}"
    cmd = QEMU_PACKAGES[pm][1]
    if pm == "apt-get":
        cmd = ["sh", "-c", "apt-get update -qq; apt-get install -y qemu-utils"]
    if os.geteuid() == 0:
        full = cmd
    elif graphical and shutil.which("pkexec"):
        full = ["pkexec", *cmd]
    elif shutil.which("sudo") and not graphical:
        full = ["sudo", *cmd]
    elif shutil.which("pkexec"):
        full = ["pkexec", *cmd]
    else:
        return False, f"Neither pkexec nor sudo is available. Run as root: {qemu_install_command()}"
    try:
        proc = subprocess.run(full, capture_output=graphical, text=True, timeout=1800,
                              stdin=subprocess.DEVNULL if graphical else None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Could not run {full[0]}: {exc}"
    if proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout or "") if graphical else "").strip().splitlines()
        reason = detail[-1] if detail else f"exit code {proc.returncode}"
        return False, f"Installing {QEMU_PACKAGES[pm][0]} failed ({reason})."
    if QemuImg.find() is None:
        return False, f"{QEMU_PACKAGES[pm][0]} was installed, but qemu-img is still not found."
    return True, f"Installed {QEMU_PACKAGES[pm][0]}."


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""
    optional: bool = False


def vboxmanage() -> str | None:
    return shutil.which("VBoxManage") or shutil.which("vboxmanage")


def doctor() -> list[Check]:
    checks: list[Check] = []
    v = sys.version_info
    checks.append(Check("Python", v >= (3, 10), f"{v.major}.{v.minor}.{v.micro}",
                        "VirtConvert&Create needs Python 3.10 or newer."))
    qemu = QemuImg.find()
    if qemu is None:
        checks.append(Check("qemu-img", False, "not found", qemu_install_command()))
    else:
        missing = qemu.missing_drivers()
        ok = not qemu.too_old and not missing
        detail = f"{qemu.version_text or 'unknown version'} ({qemu.path})"
        hint = ""
        if qemu.too_old:
            hint = "Version 2.10 or newer is needed; update your system."
        elif missing:
            hint = (f"Built without {', '.join(missing)}; install your distribution's full "
                    "qemu-img package.")
        checks.append(Check("qemu-img", ok, detail, hint))
    try:
        import PySide6  # noqa: F401
        from PySide6 import QtSvg  # noqa: F401
        checks.append(Check("PySide6 (the app)", True, PySide6.__version__, optional=True))
    except Exception as exc:  # noqa: BLE001 - any import problem means no GUI
        checks.append(Check("PySide6 (the app)", False, f"not available ({exc.__class__.__name__})",
                            "Only the command line works. Install PySide6 (see the README).",
                            optional=True))
    vbox = vboxmanage()
    detail = "not installed"
    if vbox:
        try:
            out = subprocess.run([vbox, "--version"], capture_output=True, text=True, timeout=30)
            lines = [line for line in out.stdout.splitlines() if line and line[0].isdigit()]
            detail = lines[-1] if lines else "installed"
        except (OSError, subprocess.SubprocessError):
            detail = "installed"
    checks.append(Check("VirtualBox", bool(vbox), detail,
                        "Not needed for converting; install it to run the converted disks.",
                        optional=True))
    elevate = shutil.which("pkexec")
    checks.append(Check("pkexec (disks)", bool(elevate), elevate or "not installed",
                        "The app asks for your password with it to read or write a physical disk (polkit "
                        "package); on the command line sudo does it.", optional=True))
    udisks = shutil.which("udisksctl")
    checks.append(Check("udisksctl (disks)", bool(udisks), udisks or "not installed",
                        "Unmounts a disk before writing onto it, without a password for USB sticks (udisks2 "
                        "package); without it, umount asks for the password.", optional=True))
    from .create import FILESYSTEMS  # create imports this module
    for fs in FILESYSTEMS:
        tool = find_program(*fs.tools)
        checks.append(Check(f"{fs.label} (new disks)", tool is not None, tool or "not installed",
                            f"Only needed to create {fs.label} disks: {install_command(package_for(fs.package))}",
                            optional=True))
    return checks
