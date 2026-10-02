"""Everything the controller asks of the host: virsh, qemu-img, xorriso, disk space.

The controller never opens a disk image a job VM has written to. Overlays and
scratch disks are created empty, handed to the VM, and unlinked.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Run = Callable[..., subprocess.CompletedProcess]
VIRSH = ["virsh", "-c", "qemu:///system"]

# What virsh says when the domain does not exist or has already stopped.
GONE = ("domain not found", "failed to get domain", "domain is not running")


class HypervisorError(RuntimeError):
    """virsh failed for a reason other than the domain being gone."""


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _gone(result: subprocess.CompletedProcess) -> bool:
    text = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in text for marker in GONE)


@dataclass(frozen=True)
class SlotDisks:
    overlay: Path
    scratch: Path
    seed: Path


class VirshHypervisor:
    def __init__(self, run: Run = _run, root: Path = Path("/")):
        self._run = run
        self._root = root  # where /sys and /proc are; tests point it at a copy

    def list_domains(self, prefix: str) -> list[str]:
        out = self._run([*VIRSH, "list", "--all", "--name"]).stdout
        return [name for name in out.split() if name.startswith(prefix)]

    def prepare(self, slot_dir: Path, base: Path, scratch_gib: int, seed_files: dict[str, str]) -> SlotDisks:
        self.wipe(slot_dir)
        slot_dir.mkdir(parents=True, mode=0o700)
        slot_dir.chmod(0o700)
        disks = SlotDisks(
            overlay=slot_dir / "overlay.qcow2", scratch=slot_dir / "scratch.qcow2", seed=slot_dir / "seed.iso"
        )
        self._run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(disks.overlay)])
        self._run(["qemu-img", "create", "-q", "-f", "qcow2", str(disks.scratch), f"{scratch_gib}G"])
        seed_dir = slot_dir / "seed"
        seed_dir.mkdir(mode=0o700)
        for name, content in seed_files.items():
            (seed_dir / name).write_text(content)
        try:
            self._run(
                ["xorriso", "-as", "mkisofs", "-quiet", "-r", "-V", "VRSEED", "-o", str(disks.seed), str(seed_dir)]
            )
        finally:
            shutil.rmtree(seed_dir)
        return disks

    def start(self, name: str, xml: str, slot_dir: Path) -> None:
        path = slot_dir / "domain.xml"
        path.write_text(xml)
        self._run([*VIRSH, "create", str(path)])

    def is_running(self, name: str) -> bool:
        result = self._run([*VIRSH, "domstate", name], check=False)
        if result.returncode != 0:
            if _gone(result):
                return False
            raise HypervisorError(f"virsh domstate {name}: {result.stderr.strip()}")
        return result.stdout.strip() not in ("", "shut off", "crashed")

    def destroy(self, name: str) -> None:
        """Stop the domain. A domain that is already gone is fine; any other failure raises."""
        result = self._run([*VIRSH, "destroy", name], check=False)
        if result.returncode != 0 and not _gone(result):
            raise HypervisorError(f"virsh destroy {name}: {result.stderr.strip()}")

    def wipe(self, slot_dir: Path) -> None:
        if slot_dir.exists():
            shutil.rmtree(slot_dir)

    def free_gib(self, path: Path) -> float:
        return shutil.disk_usage(path).free / 2**30

    # -- what this host has; nothing about a host is configured by hand ----------

    def numa_nodes(self) -> list[int]:
        nodes = (self._root / "sys/devices/system/node").glob("node[0-9]*")
        return sorted(int(re.sub(r"\D", "", n.name)) for n in nodes)

    def node_cpulist(self, node: int) -> str:
        return (self._root / f"sys/devices/system/node/node{node}/cpulist").read_text().strip()

    def host_cpus(self) -> int:
        return os.cpu_count() or 1

    def host_memory_gib(self) -> float:
        for line in (self._root / "proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 2**20
        raise RuntimeError("no MemTotal in /proc/meminfo")

    def host_addresses(self, exclude: tuple[str, ...]) -> list[str]:
        """This host's IPv4 addresses, leaving out loopback and the named interfaces."""
        interfaces = json.loads(self._run(["ip", "-j", "-4", "addr", "show"]).stdout)
        return [
            info["local"]
            for iface in interfaces
            if iface["ifname"] not in exclude and "LOOPBACK" not in iface.get("flags", [])
            for info in iface.get("addr_info", [])
        ]
