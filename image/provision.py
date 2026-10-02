#!/usr/bin/python3
"""Provision the runner image. Runs once, as root, inside the build VM.

cloud-init mounts the seed ISO at /mnt/payload and starts this script. It ends
by writing /etc/vivado-runner-image.json; the host treats a missing manifest as
a failed build.
"""

import datetime
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tomllib
import urllib.request

PAYLOAD = pathlib.Path("/mnt/payload")
VERSIONS = tomllib.loads((PAYLOAD / "versions.toml").read_text())
RUNNER_HOME = pathlib.Path("/home/runner/actions-runner")

APT_PACKAGES = [
    "ca-certificates", "git", "make", "xz-utils", "e2fsprogs",
    "gcc-riscv64-unknown-elf", "picolibc-riscv64-unknown-elf",
    "locales",
    # What Vivado's batch mode links against on a headless Debian 13.
    # Plan 2 Task 4 runs Vivado in the image and adds anything it reports missing.
    "libx11-6", "libxext6", "libxrender1", "libxtst6", "libxi6",
    "libfreetype6", "fontconfig", "libncurses6", "libtinfo6",
]  # fmt: skip


def sh(*cmd: str, **kwargs) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def as_runner(*cmd: str, cwd: str = "/home/runner") -> None:
    env = {
        "HOME": "/home/runner",
        "USER": "runner",
        "PATH": "/home/runner/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "UV_LINK_MODE": "copy",
    }
    print("+ (runner)", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=cwd, env=env, user="runner", group="runner", extra_groups=[])


def fetch(url: str, sha256: str, dest: pathlib.Path) -> None:
    print("+ fetch", url, flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != sha256:
        raise SystemExit(f"{url}: sha256 {digest}, expected {sha256}")


def packages() -> None:
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    sh("apt-get", "update", env=env)
    sh("apt-get", "install", "-y", "--no-install-recommends", *APT_PACKAGES, env=env)
    pathlib.Path("/etc/locale.gen").write_text("en_US.UTF-8 UTF-8\n")
    sh("locale-gen")


def runner() -> None:
    sh("useradd", "--create-home", "--shell", "/bin/bash", "runner")
    version = VERSIONS["runner"]["version"]
    tarball = pathlib.Path("/root/actions-runner.tar.gz")
    fetch(
        f"https://github.com/actions/runner/releases/download/v{version}/actions-runner-linux-x64-{version}.tar.gz",
        VERSIONS["runner"]["sha256"],
        tarball,
    )
    RUNNER_HOME.mkdir(parents=True)
    with tarfile.open(tarball) as tar:
        tar.extractall(RUNNER_HOME, filter="tar")
    tarball.unlink()
    sh(str(RUNNER_HOME / "bin" / "installdependencies.sh"), env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
    sh("chown", "-R", "runner:runner", "/home/runner")


def uv_and_python() -> None:
    version = VERSIONS["uv"]["version"]
    tarball = pathlib.Path("/root/uv.tar.gz")
    fetch(
        f"https://github.com/astral-sh/uv/releases/download/{version}/uv-x86_64-unknown-linux-gnu.tar.gz",
        VERSIONS["uv"]["sha256"],
        tarball,
    )
    with tarfile.open(tarball) as tar:
        tar.extractall("/root/uv-unpack", filter="tar")
    for name in ("uv", "uvx"):
        shutil.copy2(f"/root/uv-unpack/uv-x86_64-unknown-linux-gnu/{name}", f"/usr/local/bin/{name}")
    shutil.rmtree("/root/uv-unpack")
    tarball.unlink()
    as_runner("uv", "python", "install", VERSIONS["python"]["version"])


def warm_uv_cache() -> str:
    """Fill the uv cache from the lock, then prove a sync needs no network."""
    python = VERSIONS["python"]["version"]
    for name in ("warm", "prove"):
        project = pathlib.Path(f"/home/runner/cache-{name}")
        project.mkdir()
        for file in ("pyproject.toml", "uv.lock"):
            shutil.copy2(PAYLOAD / file, project / file)
        sh("chown", "-R", "runner:runner", str(project))
    sync = ["uv", "sync", "--frozen", "--extra", "build", "--no-install-project", "--python", python]
    as_runner(*sync, cwd="/home/runner/cache-warm")
    # A fresh project directory, same cache, no network: fails if any locked
    # distribution or build requirement is missing from the cache.
    as_runner(*sync, "--offline", cwd="/home/runner/cache-prove")
    shutil.rmtree("/home/runner/cache-warm")
    shutil.rmtree("/home/runner/cache-prove")
    return hashlib.sha256((PAYLOAD / "uv.lock").read_bytes()).hexdigest()


def guest_files() -> None:
    shutil.copy2(PAYLOAD / "vivado-runner-boot", "/usr/local/sbin/vivado-runner-boot")
    os.chmod("/usr/local/sbin/vivado-runner-boot", 0o755)
    shutil.copy2(PAYLOAD / "vivado-runner.service", "/etc/systemd/system/vivado-runner.service")
    pathlib.Path("/opt/Xilinx").mkdir(parents=True, exist_ok=True)
    sh("systemctl", "enable", "vivado-runner.service")


def network() -> None:
    """DHCP on whatever NIC the VM has (the MAC differs per slot); no IPv6."""
    for old in pathlib.Path("/etc/netplan").glob("*"):
        old.unlink()
    for old in pathlib.Path("/etc/systemd/network").glob("*"):
        old.unlink()
    pathlib.Path("/etc/systemd/network/10-runner.network").write_text(
        "[Match]\nType=ether\n\n[Network]\nDHCP=ipv4\nIPv6AcceptRA=no\nLinkLocalAddressing=no\n"
    )
    pathlib.Path("/etc/sysctl.d/90-no-ipv6.conf").write_text(
        "net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\n"
    )
    sh("systemctl", "enable", "systemd-networkd.service")


def lock_down() -> None:
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    sh("apt-get", "purge", "-y", "openssh-server", "sudo", env=env)
    sh("apt-get", "autoremove", "-y", "--purge", env=env)
    sh("apt-get", "clean")
    pathlib.Path("/etc/cloud/cloud-init.disabled").touch()
    sh("passwd", "--lock", "root")


def main() -> None:
    packages()
    runner()
    uv_and_python()
    lock_sha256 = warm_uv_cache()
    guest_files()
    network()
    lock_down()
    manifest = {
        "built": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "runner_version": VERSIONS["runner"]["version"],
        "uv_version": VERSIONS["uv"]["version"],
        "python_version": VERSIONS["python"]["version"],
        "lock_sha256": lock_sha256,
        "debian_image_sha512": (PAYLOAD / "debian-image.sha512").read_text().strip(),
    }
    pathlib.Path("/etc/vivado-runner-image.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
