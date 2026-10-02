import xml.etree.ElementTree as ET
from pathlib import Path

from vivado_runners.domain_xml import DomainSpec, render

SPEC = DomainSpec(
    name="vr-alpha-2",
    vcpus=8,
    memory_gib=24,
    mac="52:54:00:76:00:02",
    ip="192.168.76.12",
    network="vivado-runners",
    overlay=Path("/var/lib/vivado-runners/slot-2/overlay.qcow2"),
    vivado=Path("/var/lib/vivado-runners/images/vivado-2025.2-2026-10-02.squashfs"),
    seed=Path("/var/lib/vivado-runners/slot-2/seed.iso"),
    scratch=Path("/var/lib/vivado-runners/slot-2/scratch.qcow2"),
    console_log=Path("/var/lib/vivado-runners/console/slot-2.log"),
)


def root(spec=SPEC):
    return ET.fromstring(render(spec))


def disks(r):
    return {d.findtext("serial"): d for d in r.findall("devices/disk")}


def test_basics():
    r = root()
    assert r.get("type") == "kvm"
    assert r.findtext("name") == "vr-alpha-2"
    assert (r.find("memory").get("unit"), r.findtext("memory")) == ("GiB", "24")
    assert r.findtext("vcpu") == "8"
    assert r.find("vcpu").get("cpuset") is None
    assert r.find("numatune") is None


def test_every_guest_exit_destroys_the_domain():
    r = root()
    assert [r.findtext(t) for t in ("on_poweroff", "on_reboot", "on_crash")] == ["destroy"] * 3


def test_disks_have_serials_and_read_only_where_required():
    d = disks(root())
    assert set(d) == {None, "vr-vivado", "vr-seed", "vr-scratch"}
    overlay = d[None]
    assert overlay.find("source").get("file").endswith("slot-2/overlay.qcow2")
    assert overlay.find("driver").get("type") == "qcow2"
    assert overlay.find("readonly") is None
    assert d["vr-vivado"].find("readonly") is not None
    assert d["vr-vivado"].find("driver").get("type") == "raw"
    assert d["vr-seed"].find("readonly") is not None
    assert d["vr-scratch"].find("readonly") is None
    assert [x.find("target").get("dev") for x in root().findall("devices/disk")] == ["vda", "vdb", "vdc", "vdd"]


def test_nic_is_isolated_filtered_and_on_the_runner_network():
    nic = root().find("devices/interface")
    assert nic.get("type") == "network"
    assert nic.find("source").get("network") == "vivado-runners"
    assert nic.find("mac").get("address") == "52:54:00:76:00:02"
    assert nic.find("port").get("isolated") == "yes"
    ref = nic.find("filterref")
    assert ref.get("filter") == "clean-traffic"
    assert (ref.find("parameter").get("name"), ref.find("parameter").get("value")) == ("IP", "192.168.76.12")


def test_no_graphics_usb_balloon_or_host_devices():
    r = root()
    assert r.find("devices/graphics") is None
    assert r.find("devices/hostdev") is None
    assert r.find("devices/filesystem") is None
    assert r.find("devices/memballoon").get("model") == "none"
    usb = [c for c in r.findall("devices/controller") if c.get("type") == "usb"]
    assert [c.get("model") for c in usb] == ["none"]


def test_console_goes_to_a_log_file():
    serial = root().find("devices/serial")
    assert serial.get("type") == "pty"
    assert serial.find("log").get("file") == "/var/lib/vivado-runners/console/slot-2.log"
    assert serial.find("log").get("append") == "off"


def test_numa_pinning_when_configured():
    from dataclasses import replace

    r = root(replace(SPEC, cpuset="0-3,8-11", numa_node=0))
    assert r.find("vcpu").get("cpuset") == "0-3,8-11"
    mem = r.find("numatune/memory")
    assert (mem.get("mode"), mem.get("nodeset")) == ("strict", "0")
