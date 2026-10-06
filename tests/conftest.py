import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="vertconvert-tests-")

import pytest  # noqa: E402

from vertconvert.core.qemu import QemuImg  # noqa: E402


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
