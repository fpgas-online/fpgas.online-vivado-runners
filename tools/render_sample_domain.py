"""Print the domain XML for a sample slot, for `virt-xml-validate` in CI."""

from pathlib import Path

from vivado_runners.domain_xml import DomainSpec, render

print(
    render(
        DomainSpec(
            name="vr-sample-0",
            vcpus=8,
            memory_gib=24,
            mac="52:54:00:76:00:00",
            ip="192.168.76.10",
            network="vivado-runners",
            overlay=Path("/var/lib/vivado-runners/slot-0/overlay.qcow2"),
            vivado=Path("/var/lib/vivado-runners/images/vivado-2025.2-2026-10-02.squashfs"),
            seed=Path("/var/lib/vivado-runners/slot-0/seed.iso"),
            scratch=Path("/var/lib/vivado-runners/slot-0/scratch.qcow2"),
            console_log=Path("/var/lib/vivado-runners/console/slot-0.log"),
            cpuset="0-3,8-11",
            numa_node=0,
        )
    )
)
