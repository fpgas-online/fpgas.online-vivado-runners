"""libvirt domain XML for one slot's VM.

The VM gets four virtio disks (root overlay, Vivado, seed, scratch), one NIC on
the isolated runner network, a serial console and nothing else. Any way the
guest stops (poweroff, reboot, crash) destroys the domain, so a VM never runs a
second job.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DomainSpec:
    name: str
    vcpus: int
    memory_gib: int
    mac: str
    ip: str
    network: str
    overlay: Path
    vivado: Path
    seed: Path
    scratch: Path
    console_log: Path
    cpuset: str | None = None
    numa_node: int | None = None


def _disk(devices: ET.Element, path: Path, fmt: str, dev: str, serial: str | None, readonly: bool) -> None:
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type=fmt, discard="unmap")
    ET.SubElement(disk, "source", file=str(path))
    ET.SubElement(disk, "target", dev=dev, bus="virtio")
    if serial:
        ET.SubElement(disk, "serial").text = serial
    if readonly:
        ET.SubElement(disk, "readonly")


def render(spec: DomainSpec) -> str:
    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = spec.name
    ET.SubElement(domain, "memory", unit="GiB").text = str(spec.memory_gib)
    vcpu = ET.SubElement(domain, "vcpu", placement="static")
    vcpu.text = str(spec.vcpus)
    if spec.cpuset:
        vcpu.set("cpuset", spec.cpuset)
    if spec.numa_node is not None:
        numatune = ET.SubElement(domain, "numatune")
        ET.SubElement(numatune, "memory", mode="strict", nodeset=str(spec.numa_node))

    os_ = ET.SubElement(domain, "os")
    ET.SubElement(os_, "type", arch="x86_64", machine="q35").text = "hvm"
    ET.SubElement(os_, "boot", dev="hd")
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    ET.SubElement(domain, "cpu", mode="host-passthrough")
    for event in ("on_poweroff", "on_reboot", "on_crash"):
        ET.SubElement(domain, event).text = "destroy"

    devices = ET.SubElement(domain, "devices")
    _disk(devices, spec.overlay, "qcow2", "vda", None, readonly=False)
    _disk(devices, spec.vivado, "raw", "vdb", "vr-vivado", readonly=True)
    _disk(devices, spec.seed, "raw", "vdc", "vr-seed", readonly=True)
    _disk(devices, spec.scratch, "qcow2", "vdd", "vr-scratch", readonly=False)

    nic = ET.SubElement(devices, "interface", type="network")
    ET.SubElement(nic, "source", network=spec.network)
    ET.SubElement(nic, "mac", address=spec.mac)
    ET.SubElement(nic, "model", type="virtio")
    ET.SubElement(nic, "port", isolated="yes")
    ref = ET.SubElement(nic, "filterref", filter="clean-traffic")
    ET.SubElement(ref, "parameter", name="IP", value=spec.ip)

    serial = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial, "log", file=str(spec.console_log), append="off")
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")

    ET.SubElement(devices, "controller", type="usb", model="none")
    ET.SubElement(devices, "memballoon", model="none")
    rng = ET.SubElement(devices, "rng", model="virtio")
    ET.SubElement(rng, "backend", model="random").text = "/dev/urandom"

    ET.indent(domain)
    return ET.tostring(domain, encoding="unicode")
