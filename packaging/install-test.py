#!/usr/bin/python3
"""Install the built deb in a clean container and check what it promises."""

import glob
import grp
import os
import pwd
import stat
import subprocess
import sys

ENV = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}


def sh(*cmd: str) -> str:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, capture_output=True, text=True, env=ENV).stdout


def check(condition: bool, message: str) -> None:
    print(("ok   " if condition else "FAIL ") + message, flush=True)
    if not condition:
        sys.exit(1)


sh("apt-get", "update")
sh("apt-get", "install", "-y", "--no-install-recommends", *glob.glob("/debs/vivado-runners_*.deb"))

check("usage: vivado-runners" in sh("vivado-runners", "--help"), "the CLI runs")

user = pwd.getpwnam("vivado-runners")
groups = {g.gr_name for g in grp.getgrall() if "vivado-runners" in g.gr_mem}
check({"libvirt", "libvirt-qemu"} <= groups, f"user is in libvirt and libvirt-qemu (has {sorted(groups)})")

images = os.stat("/var/lib/vivado-runners/images")
check(stat.S_IMODE(images.st_mode) == 0o750, "images directory is 0750")
check(images.st_uid == user.pw_uid, "images directory belongs to the controller")
check(grp.getgrgid(images.st_gid).gr_name == "libvirt-qemu", "images directory group is libvirt-qemu")

for path in (
    "/usr/share/vivado-runners/host/network.xml",
    "/usr/share/vivado-runners/host/vivado-runners.nft",
    "/usr/share/vivado-runners/image/build_image.py",
    "/usr/share/vivado-runners/image/guest/vivado-runner-boot",
    "/etc/vivado-runners/squid.conf",
    "/etc/vivado-runners/allowed-hosts",
    "/etc/vivado-runners/allowed-blob-regex",
):
    check(os.path.isfile(path), f"{path} is installed")

for unit in ("vivado-runners.service", "vivado-runners-proxy.service", "vivado-runners-firewall.service",
             "vivado-runners-firewall.timer", "vivado-runners-firewall-assert.service"):  # fmt: skip
    check(os.path.isfile(f"/usr/lib/systemd/system/{unit}"), f"{unit} is installed")
    enabled = glob.glob(f"/etc/systemd/system/*.wants/{unit}")
    check(not enabled, f"{unit} is not enabled by the package")

sh("squid", "-k", "parse", "-f", "/etc/vivado-runners/squid.conf")
check(True, "the installed squid configuration parses")
print("install test passed")
