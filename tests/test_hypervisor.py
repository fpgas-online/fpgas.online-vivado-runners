import json
import subprocess
from pathlib import Path

import pytest

from vivado_runners.hypervisor import HypervisorError, SlotDisks, VirshHypervisor


class Shell:
    """Records commands; answers from `outputs`, keyed by the first two words."""

    def __init__(self, outputs=None, fail=(), stderr: str = ""):
        self.cmds = []
        self.outputs = outputs or {}
        self.fail = set(fail)
        self.stderr = stderr

    def __call__(self, cmd, check=True):
        self.cmds.append(cmd)
        key = " ".join(cmd[:4])
        code = 1 if any(f in key for f in self.fail) else 0
        if code and check:
            raise subprocess.CalledProcessError(code, cmd)
        out = next((v for k, v in self.outputs.items() if k in " ".join(cmd)), "")
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=self.stderr if code else "")


def test_list_domains_filters_by_prefix():
    sh = Shell({"list --all --name": "vr-alpha-0\nvr-alpha-1\ndocker\n\n"})
    assert VirshHypervisor(sh).list_domains("vr-alpha-") == ["vr-alpha-0", "vr-alpha-1"]
    assert sh.cmds == [["virsh", "-c", "qemu:///system", "list", "--all", "--name"]]


def test_prepare_creates_overlay_scratch_and_seed(tmp_path):
    sh = Shell()
    slot = tmp_path / "slot-0"
    base = Path("/images/runner-base-2026-10-02.1.qcow2")
    disks = VirshHypervisor(sh).prepare(slot, base, 60, {"jitconfig": "abc==", "proxy": "http://192.168.76.1:3128"})
    assert disks == SlotDisks(overlay=slot / "overlay.qcow2", scratch=slot / "scratch.qcow2", seed=slot / "seed.iso")
    assert sh.cmds[0] == ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(disks.overlay)]
    assert sh.cmds[1] == ["qemu-img", "create", "-q", "-f", "qcow2", str(disks.scratch), "60G"]
    assert sh.cmds[2][:2] == ["xorriso", "-as"]
    assert sh.cmds[2][-1] == str(slot / "seed")
    assert sh.cmds[2][sh.cmds[2].index("-V") : sh.cmds[2].index("-V") + 2] == ["-V", "VRSEED"]
    assert not (slot / "seed").exists(), "the plaintext seed directory is removed once the ISO exists"
    assert oct(slot.stat().st_mode & 0o777) == "0o700"


def test_prepare_writes_seed_files_before_building_the_iso(tmp_path):
    seen = {}

    def sh(cmd, check=True):
        if cmd[0] == "xorriso":
            seed = Path(cmd[-1])
            seen.update({p.name: p.read_text() for p in seed.iterdir()})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    VirshHypervisor(sh).prepare(tmp_path / "slot-0", Path("/b.qcow2"), 1, {"jitconfig": "abc==", "proxy": "p"})
    assert seen == {"jitconfig": "abc==", "proxy": "p"}


def test_prepare_starts_from_an_empty_slot_dir(tmp_path):
    slot = tmp_path / "slot-0"
    slot.mkdir()
    (slot / "leftover").write_text("x")
    VirshHypervisor(Shell()).prepare(slot, Path("/b.qcow2"), 1, {})
    assert not (slot / "leftover").exists()


def test_start_writes_the_xml_and_creates_a_transient_domain(tmp_path):
    sh = Shell()
    slot = tmp_path / "slot-0"
    slot.mkdir()
    VirshHypervisor(sh).start("vr-h-0", "<domain/>", slot)
    assert (slot / "domain.xml").read_text() == "<domain/>"
    assert sh.cmds == [["virsh", "-c", "qemu:///system", "create", str(slot / "domain.xml")]]


def test_is_running_reads_domstate_and_treats_a_missing_domain_as_stopped():
    assert VirshHypervisor(Shell({"domstate": "running\n"})).is_running("vr-h-0") is True
    assert VirshHypervisor(Shell({"domstate": "paused\n"})).is_running("vr-h-0") is True
    assert VirshHypervisor(Shell({"domstate": "shut off\n"})).is_running("vr-h-0") is False
    stderr_msg = "error: failed to get domain 'vr-h-0'\nerror: Domain not found: no domain with matching name 'vr-h-0'"
    assert VirshHypervisor(Shell(fail=["domstate"], stderr=stderr_msg)).is_running("vr-h-0") is False


def test_destroy_ignores_a_domain_that_is_already_gone():
    stderr_msg = (
        "error: Failed to destroy domain 'vr-h-0'\nerror: Requested operation is not valid: domain is not running"
    )
    sh = Shell(fail=["destroy"], stderr=stderr_msg)
    VirshHypervisor(sh).destroy("vr-h-0")
    assert sh.cmds == [["virsh", "-c", "qemu:///system", "destroy", "vr-h-0"]]


def test_is_running_raises_when_libvirt_cannot_be_asked():
    with pytest.raises(HypervisorError, match="failed to connect"):
        VirshHypervisor(Shell(fail=["domstate"], stderr="error: failed to connect to the hypervisor")).is_running(
            "vr-h-0"
        )


def test_destroy_raises_when_libvirt_cannot_be_asked():
    with pytest.raises(HypervisorError, match="failed to connect"):
        VirshHypervisor(Shell(fail=["destroy"], stderr="error: failed to connect to the hypervisor")).destroy("vr-h-0")


def test_wipe_removes_the_directory_and_tolerates_its_absence(tmp_path):
    slot = tmp_path / "slot-0"
    slot.mkdir()
    (slot / "overlay.qcow2").write_text("x")
    hv = VirshHypervisor(Shell())
    hv.wipe(slot)
    hv.wipe(slot)
    assert not slot.exists()


def test_free_gib_reports_the_filesystem(tmp_path):
    assert VirshHypervisor(Shell()).free_gib(tmp_path) > 0


def fake_host(tmp_path, nodes, mem_kb=64 * 2**20):
    for node, cpus in nodes.items():
        d = tmp_path / f"sys/devices/system/node/node{node}"
        d.mkdir(parents=True)
        (d / "cpulist").write_text(cpus + "\n")
    (tmp_path / "sys/devices/system/node").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sys/devices/system/node/possible").write_text("0-1\n")
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/meminfo").write_text(f"MemTotal:       {mem_kb} kB\nMemFree:         1000 kB\n")
    return tmp_path


def test_numa_layout_is_read_from_the_host(tmp_path):
    hv = VirshHypervisor(Shell(), root=fake_host(tmp_path, {0: "0-3,8-11", 1: "4-7,12-15", 10: "16-19"}))
    assert hv.numa_nodes() == [0, 1, 10]
    assert hv.node_cpulist(1) == "4-7,12-15"


def test_a_host_without_numa_information_has_no_nodes(tmp_path):
    assert VirshHypervisor(Shell(), root=fake_host(tmp_path, {})).numa_nodes() == []


def test_host_memory_is_read_from_meminfo(tmp_path):
    hv = VirshHypervisor(Shell(), root=fake_host(tmp_path, {0: "0-11"}, mem_kb=32 * 2**20))
    assert hv.host_memory_gib() == 32.0
    assert hv.host_cpus() >= 1


def test_host_addresses_leave_out_loopback_and_the_runner_bridge():
    interfaces = [
        {"ifname": "lo", "flags": ["LOOPBACK", "UP"], "addr_info": [{"local": "127.0.0.1"}]},
        {"ifname": "eth0", "flags": ["UP"], "addr_info": [{"local": "203.0.113.7"}, {"local": "203.0.113.8"}]},
        {"ifname": "virbr0", "flags": ["UP"], "addr_info": [{"local": "198.51.100.1"}]},
        {"ifname": "vrbr0", "flags": ["UP"], "addr_info": [{"local": "192.168.76.1"}]},
        {"ifname": "eth1", "flags": ["UP"], "addr_info": []},
    ]
    sh = Shell({"addr show": json.dumps(interfaces)})
    assert VirshHypervisor(sh).host_addresses(exclude=("vrbr0",)) == ["203.0.113.7", "203.0.113.8", "198.51.100.1"]
    assert sh.cmds == [["ip", "-j", "-4", "addr", "show"]]
