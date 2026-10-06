import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="virtconvert-tests-")

import pytest  # noqa: E402

from virtconvert.core.qemu import QemuImg  # noqa: E402


@pytest.fixture(scope="session")
def qemu():
    q = QemuImg.find()
    if q is None:
        pytest.skip("qemu-img is not installed")
    return q


def vboxmanage():
    return shutil.which("VBoxManage") or shutil.which("vboxmanage")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "qemu" in item.fixturenames:
            item.add_marker(pytest.mark.qemu)


@pytest.fixture
def fake_disk(tmp_path, monkeypatch):
    """Regular files standing in for physical disks (USB sticks full of old contents), so writing
    onto a disk runs end to end without root: make(size) -> devices.PhysicalDisk."""
    from virtconvert.core import devices

    disks: dict[str, devices.PhysicalDisk] = {}
    real_is_device, real_open = devices.is_device, devices.open_device

    def make(size: int, name: str = "sdz", fill: bytes = b"\xee", **fields) -> devices.PhysicalDisk:
        path = tmp_path / f"{name}.disk"
        path.write_bytes(fill * size)
        fields = {"model": "Test Stick", "serial": "T-1", "transport": "usb", "removable": True, **fields}
        disk = devices.PhysicalDisk(path=str(path), name=name, size=size, **fields)
        disks[str(path)] = disk
        return disk

    def open_device(path, how="auto", cancel=None, timeout=600, _helper_args=(), write=False):
        if str(path) in disks:
            return os.open(path, os.O_RDWR if write else os.O_RDONLY)
        return real_open(path, how, cancel, timeout, _helper_args, write)

    monkeypatch.setattr(devices, "is_device", lambda p: str(p) in disks or real_is_device(p))
    monkeypatch.setattr(devices, "disk_info", lambda p: disks[str(p)])
    monkeypatch.setattr(devices, "base_disks", lambda p: {disks[str(p)].name} if str(p) in disks else set())
    monkeypatch.setattr(devices, "disks_holding", lambda p: set())
    monkeypatch.setattr(devices, "open_device", open_device)
    monkeypatch.setattr(devices, "list_disks", lambda include_loop=False: list(disks.values()))
    make.disks = disks
    return make
