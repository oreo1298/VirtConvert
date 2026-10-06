"""Virtual appliances: OVA files (a tar archive) and OVF descriptors.

An OVA holds an OVF descriptor (XML: the machine and its disks), the disk
files (usually stream-optimized VMDK) and a manifest of checksums. The tar
format stores files uncompressed and in one piece, so the disks are read in
place: qemu-img gets the disk's offset and size inside the archive.

Writing an OVA makes a minimal OVF 1.0 machine (CPUs, memory, a SATA disk, a
NAT network card) around the converted disk, which VirtualBox and VMware
import with File → Import Appliance.
"""

from __future__ import annotations

import hashlib
import posixpath
import tarfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import ImageFormatError, OperationCancelled

OVF_NS = "http://schemas.dmtf.org/ovf/envelope/1"
DISK_EXTENSIONS = (".vmdk", ".vhd", ".vhdx", ".vdi", ".img", ".raw", ".qcow2", ".iso")
STREAM_VMDK_URL = "http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"
USTAR_LIMIT = 8 << 30


@dataclass
class ApplianceDisk:
    name: str                    # file name in the OVA, or next to the OVF
    capacity: int | None = None  # bytes, from the descriptor
    compression: str = ""        # "gzip" means the disk is stored compressed
    offset: int | None = None    # OVA: where its data starts in the archive
    size: int | None = None      # OVA: its length in the archive
    sparse: bool = False         # OVA: stored as a sparse tar member (its bytes aren't in one piece)
    cut_off: bool = False        # OVA: the archive ends before the member does


def _attr(el: ET.Element, name: str) -> str | None:
    """An OVF attribute, with or without the ovf: namespace (both are found in the wild)."""
    for key in (f"{{{OVF_NS}}}{name}", name):
        if key in el.attrib:
            return el.attrib[key]
    for key, value in el.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return value
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


_UNITS = {"byte": 1, "byte * 2^10": 1 << 10, "byte * 2^20": 1 << 20, "byte * 2^30": 1 << 30,
          "byte * 2^40": 1 << 40, "kilobytes": 1 << 10, "megabytes": 1 << 20, "gigabytes": 1 << 30}


def parse_ovf(data: bytes) -> list[ApplianceDisk]:
    """The disks an OVF descriptor lists, in its order."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ImageFormatError(f"the OVF descriptor is not valid XML ({exc})") from None
    files: dict[str, tuple[str, str]] = {}
    for el in root.iter():
        if _local(el.tag) == "File":
            fid, href = _attr(el, "id"), _attr(el, "href")
            if fid and href:
                files[fid] = (href, _attr(el, "compression") or "")
    disks: list[ApplianceDisk] = []
    for el in root.iter():
        if _local(el.tag) != "Disk":
            continue
        ref = _attr(el, "fileRef")
        if not ref or ref not in files:
            continue        # an empty disk the appliance creates on import
        href, compression = files[ref]
        capacity = None
        try:
            unit = (_attr(el, "capacityAllocationUnits") or "byte").lower().replace(" ", "")
            factor = next((v for k, v in _UNITS.items() if k.replace(" ", "") == unit), 1)
            capacity = int(_attr(el, "capacity") or 0) * factor or None
        except ValueError:
            pass
        disks.append(ApplianceDisk(name=href, capacity=capacity, compression=compression))
    return disks


def is_tar(head: bytes) -> bool:
    return len(head) > 262 and head[257:262] == b"ustar"


def _norm(name: str) -> str:
    """A member name as the archive's own path: ./a.vmdk and a.vmdk are the same."""
    return posixpath.normpath(name.replace("\\", "/").lstrip("/"))


def list_ova(path: Path) -> list[ApplianceDisk]:
    """The disks in an OVA, with their place in the archive."""
    try:
        archive_size = path.stat().st_size
        with tarfile.open(path, "r:") as tar:
            members = {_norm(m.name): m for m in tar.getmembers() if m.isfile()}
            descriptor = next((n for n in members if n.lower().endswith(".ovf")), None)
            disks = parse_ovf(tar.extractfile(members[descriptor]).read()) if descriptor is not None else []
    except (tarfile.TarError, OSError) as exc:
        raise ImageFormatError(f"{path.name} is not a readable OVA archive ({exc})") from None
    if descriptor is None or not disks:
        # no usable descriptor: take the disk files themselves
        disks = [ApplianceDisk(name=n) for n in members if n.lower().endswith(DISK_EXTENSIONS)]
    else:
        # hrefs are relative to the descriptor
        for disk in disks:
            disk.name = _norm(posixpath.join(posixpath.dirname(descriptor), disk.name.replace("\\", "/")))
    found = []
    for disk in disks:
        member = members.get(disk.name)
        if member is None:
            raise ImageFormatError(f"{path.name} lists {disk.name}, but the archive doesn't contain it")
        disk.offset, disk.size = member.offset_data, member.size
        disk.sparse = member.sparse is not None
        disk.cut_off = member.offset_data + member.size > archive_size
        found.append(disk)
    return found


def list_ovf(path: Path) -> list[Path]:
    """The disk files an OVF descriptor refers to (next to it)."""
    try:
        disks = parse_ovf(path.read_bytes())
    except OSError as exc:
        raise ImageFormatError(f"Cannot read {path.name}: {exc.strerror or exc}") from None
    return [path.parent / d.name.replace("\\", "/") for d in disks]


# ---------------------------------------------------------------------- writing

def _ovf(name: str, disk_file: str, disk_size: int, capacity: int, firmware: str, os_hint: str) -> bytes:
    windows = os_hint == "windows"
    memory = 4096 if windows else 2048
    os_id, os_text, vbox_os = {"windows": ("103", "Microsoft Windows (64-bit)", "Windows10_64"),
                               "linux": ("101", "Linux (64-bit)", "Linux_64")}.get(
        os_hint, ("102", "Other (64-bit)", "Other_64"))
    efi = '\n        <vmw:Config ovf:required="false" vmw:key="firmware" vmw:value="efi"/>' if firmware == "uefi" else ""
    esc = _xml_escape
    text = f'''<?xml version="1.0" encoding="UTF-8"?>
<Envelope ovf:version="1.0" xml:lang="en-US" xmlns="http://schemas.dmtf.org/ovf/envelope/1"
    xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
    xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
    xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:vbox="http://www.virtualbox.org/ovf/machine"
    xmlns:vmw="http://www.vmware.com/schema/ovf">
  <References>
    <File ovf:id="file1" ovf:href="{esc(disk_file)}" ovf:size="{disk_size}"/>
  </References>
  <DiskSection>
    <Info>List of the virtual disks used in the package</Info>
    <Disk ovf:capacity="{capacity}" ovf:diskId="vmdisk1" ovf:fileRef="file1" ovf:format="{STREAM_VMDK_URL}"/>
  </DiskSection>
  <NetworkSection>
    <Info>Logical networks used in the package</Info>
    <Network ovf:name="NAT">
      <Description>Logical network used by this appliance.</Description>
    </Network>
  </NetworkSection>
  <VirtualSystem ovf:id="{esc(name)}">
    <Info>A virtual machine</Info>
    <OperatingSystemSection ovf:id="{os_id}">
      <Info>The kind of installed guest operating system</Info>
      <Description>{os_text}</Description>
      <vbox:OSType ovf:required="false">{vbox_os}</vbox:OSType>
    </OperatingSystemSection>
    <VirtualHardwareSection>
      <Info>Virtual hardware requirements for a virtual machine</Info>
      <System>
        <vssd:ElementName>Virtual Hardware Family</vssd:ElementName>
        <vssd:InstanceID>0</vssd:InstanceID>
        <vssd:VirtualSystemIdentifier>{esc(name)}</vssd:VirtualSystemIdentifier>
        <vssd:VirtualSystemType>vmx-07 virtualbox-2.2</vssd:VirtualSystemType>
      </System>
      <Item>
        <rasd:Caption>2 virtual CPU</rasd:Caption>
        <rasd:Description>Number of virtual CPUs</rasd:Description>
        <rasd:ElementName>2 virtual CPU</rasd:ElementName>
        <rasd:InstanceID>1</rasd:InstanceID>
        <rasd:ResourceType>3</rasd:ResourceType>
        <rasd:VirtualQuantity>2</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:AllocationUnits>MegaBytes</rasd:AllocationUnits>
        <rasd:Caption>{memory} MB of memory</rasd:Caption>
        <rasd:Description>Memory Size</rasd:Description>
        <rasd:ElementName>{memory} MB of memory</rasd:ElementName>
        <rasd:InstanceID>2</rasd:InstanceID>
        <rasd:ResourceType>4</rasd:ResourceType>
        <rasd:VirtualQuantity>{memory}</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:Address>0</rasd:Address>
        <rasd:Caption>sataController0</rasd:Caption>
        <rasd:Description>SATA Controller</rasd:Description>
        <rasd:ElementName>sataController0</rasd:ElementName>
        <rasd:InstanceID>3</rasd:InstanceID>
        <rasd:ResourceSubType>AHCI</rasd:ResourceSubType>
        <rasd:ResourceType>20</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:AddressOnParent>0</rasd:AddressOnParent>
        <rasd:Caption>disk1</rasd:Caption>
        <rasd:Description>Disk Image</rasd:Description>
        <rasd:ElementName>disk1</rasd:ElementName>
        <rasd:HostResource>/disk/vmdisk1</rasd:HostResource>
        <rasd:InstanceID>4</rasd:InstanceID>
        <rasd:Parent>3</rasd:Parent>
        <rasd:ResourceType>17</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>
        <rasd:Caption>Ethernet adapter on 'NAT'</rasd:Caption>
        <rasd:Connection>NAT</rasd:Connection>
        <rasd:ElementName>Ethernet adapter on 'NAT'</rasd:ElementName>
        <rasd:InstanceID>5</rasd:InstanceID>
        <rasd:ResourceSubType>E1000</rasd:ResourceSubType>
        <rasd:ResourceType>10</rasd:ResourceType>
      </Item>{efi}
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>
'''
    return text.encode("utf-8")


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class _Watched:
    """A file read in pieces that counts what was read and stops when cancelled."""

    def __init__(self, f, step: Callable[[int], None], cancel: Callable[[], bool]):
        self.f, self.step, self.cancel = f, step, cancel

    def read(self, n: int = -1) -> bytes:
        if self.cancel():
            raise OperationCancelled()
        data = self.f.read(n if n is not None and n >= 0 else 1 << 20)
        self.step(len(data))
        return data


def _sha256(f: _Watched) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def write_ova(out: Path, vmdk: Path, name: str, capacity: int, firmware: str = "", os_hint: str = "",
              progress: Callable[[int, int], None] | None = None,
              cancel: Callable[[], bool] | None = None) -> None:
    """Pack VMDK (stream-optimized) into the appliance OUT with a descriptor and manifest.

    FIRMWARE "uefi" asks VMware for EFI; OS_HINT ("windows", "linux") picks the guest type.
    PROGRESS(done, total) follows the two passes over the disk (checksum, packing).
    """
    cancel = cancel or (lambda: False)
    total = 2 * vmdk.stat().st_size
    done = 0

    def step(n: int) -> None:
        nonlocal done
        done += n
        if progress is not None:
            progress(done, total)

    disk_name = vmdk.name
    ovf_name = f"{name}.ovf"
    descriptor = _ovf(name, disk_name, vmdk.stat().st_size, capacity, firmware, os_hint)
    ovf_path = vmdk.with_name(ovf_name)
    ovf_path.write_bytes(descriptor)
    with open(vmdk, "rb") as f:
        disk_sum = _sha256(_Watched(f, step, cancel))
    manifest = (f"SHA256({ovf_name})= {hashlib.sha256(descriptor).hexdigest()}\n"
                f"SHA256({disk_name})= {disk_sum}\n").encode()
    mf_path = vmdk.with_name(f"{name}.mf")
    mf_path.write_bytes(manifest)
    try:
        # the descriptor comes first, then the files in the order it lists them, then the manifest;
        # plain ustar holds files up to 8 GB, larger ones get GNU's binary size field, which
        # VirtualBox, VMware and tar read as well
        big = vmdk.stat().st_size >= USTAR_LIMIT
        with tarfile.open(out, "w", format=tarfile.GNU_FORMAT if big else tarfile.USTAR_FORMAT) as tar:
            for path in (ovf_path, vmdk, mf_path):
                info = tar.gettarinfo(str(path), arcname=path.name)
                info.uid = info.gid = 64
                info.uname = info.gname = "someone"
                info.mode = 0o644
                with open(path, "rb") as f:
                    tar.addfile(info, _Watched(f, step, cancel) if path == vmdk else f)
    finally:
        ovf_path.unlink(missing_ok=True)
        mf_path.unlink(missing_ok=True)


def appliance_name(stem: str) -> str:
    """A name for the machine and the files in the archive: plain characters, and short enough
    (in bytes) for a tar name with "-disk1.vmdk" added."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem).strip("._")
    cleaned = cleaned.encode("utf-8")[:80].decode("utf-8", "ignore").strip("._")
    return cleaned or "appliance"
