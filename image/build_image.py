#!/usr/bin/env python3
"""Build a versioned runner base image.

    uv run python image/build_image.py \\
        --images-dir /var/lib/vivado-runners/images \\
        --lock ~/fpgas.online-test-designs/uv.lock \\
        --pyproject ~/fpgas.online-test-designs/pyproject.toml

Boots Debian's cloud image once on a network WITH internet access (libvirt's
`default`), lets cloud-init run image/provision.py, and stores the result as
runner-base-<date>.<n>.qcow2. It never touches runner-base-current: select the
new image with `vivado-runners images activate base <name>` once it is tested.

Needs: qemu-utils, xorriso, virtinst, libguestfs-tools, a libvirt `default`
network, and membership of the `libvirt` group.
"""

import argparse
import datetime
import hashlib
import pathlib
import shutil
import subprocess
import sys
import tomllib
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from vivado_runners import images  # noqa: E402

VERSIONS = tomllib.loads((HERE / "versions.toml").read_text())
MANIFEST = "/etc/vivado-runner-image.json"
DOMAIN = "vr-image-build"


def sh(*cmd: str) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def user_data() -> str:
    return (
        "#cloud-config\n"
        "runcmd:\n"
        "  - mkdir -p /mnt/payload\n"
        "  - mount -o ro /dev/disk/by-label/cidata /mnt/payload\n"
        "  - python3 /mnt/payload/provision.py\n"
        "power_state:\n"
        "  mode: poweroff\n"
        "  condition: true\n"
    )


def expected_sha512(sums: str, name: str) -> str:
    for line in sums.splitlines():
        digest, _, listed = line.partition("  ")
        if listed == name:
            return digest
    raise SystemExit(f"{name} is not listed in SHA512SUMS")


def write_seed(seed: pathlib.Path, lock: pathlib.Path, pyproject: pathlib.Path, image_sha512: str) -> None:
    seed.mkdir(parents=True)
    (seed / "user-data").write_text(user_data())
    (seed / "meta-data").write_text(f"instance-id: {DOMAIN}\nlocal-hostname: vivado-runner\n")
    (seed / "debian-image.sha512").write_text(image_sha512 + "\n")
    shutil.copy2(lock, seed / "uv.lock")
    shutil.copy2(pyproject, seed / "pyproject.toml")
    for name in ("versions.toml", "provision.py"):
        shutil.copy2(HERE / name, seed / name)
    for name in ("vivado-runner-boot", "vivado-runner.service"):
        shutil.copy2(HERE / "guest" / name, seed / name)


def download(work: pathlib.Path) -> tuple[pathlib.Path, str]:
    base_url, name = VERSIONS["debian"]["base_url"], VERSIONS["debian"]["image"]
    with urllib.request.urlopen(f"{base_url}/SHA512SUMS", timeout=60) as response:
        want = expected_sha512(response.read().decode(), name)
    dest = work / name
    print("+ fetch", f"{base_url}/{name}", flush=True)
    with urllib.request.urlopen(f"{base_url}/{name}", timeout=600) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)
    digest = hashlib.sha512()
    with open(dest, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != want:
        raise SystemExit(f"{name}: sha512 {digest.hexdigest()}, expected {want}")
    return dest, want


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images-dir", type=pathlib.Path, required=True)
    parser.add_argument("--lock", type=pathlib.Path, required=True, help="uv.lock of fpgas.online-test-designs main")
    parser.add_argument("--pyproject", type=pathlib.Path, required=True)
    parser.add_argument("--work-dir", type=pathlib.Path, default=None, help="default: <images-dir>/../build")
    parser.add_argument("--network", default="default", help="libvirt network with internet access")
    parser.add_argument("--disk-gib", type=int, default=20)
    args = parser.parse_args()

    work = args.work_dir or args.images_dir.parent / "build"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    cloud_image, image_sha512 = download(work)
    disk = work / "work.qcow2"
    sh("qemu-img", "convert", "-O", "qcow2", str(cloud_image), str(disk))
    sh("qemu-img", "resize", str(disk), f"{args.disk_gib}G")

    write_seed(work / "seed", args.lock, args.pyproject, image_sha512)
    seed_iso = work / "seed.iso"
    sh("xorriso", "-as", "mkisofs", "-quiet", "-r", "-J", "-V", "cidata", "-o", str(seed_iso), str(work / "seed"))

    sh(
        "virt-install", "--connect", "qemu:///system", "--name", DOMAIN, "--transient",
        "--memory", "8192", "--vcpus", "4", "--import", "--osinfo", "debian13",
        "--disk", f"path={disk},format=qcow2,bus=virtio",
        "--disk", f"path={seed_iso},device=cdrom",
        "--network", f"network={args.network},model=virtio",
        "--graphics", "none", "--noautoconsole", "--wait", "60",
    )  # fmt: skip

    manifest = subprocess.run(["virt-cat", "-a", str(disk), MANIFEST], capture_output=True, text=True, check=False)
    if manifest.returncode != 0:
        print(manifest.stderr, file=sys.stderr)
        raise SystemExit(f"provisioning did not finish: {MANIFEST} is missing from the image. Disk kept at {disk}")
    print(manifest.stdout)

    today = datetime.date.today().isoformat()
    name = images.next_base_name(args.images_dir, today)
    final = args.images_dir / name
    sh("qemu-img", "convert", "-c", "-O", "qcow2", str(disk), str(final))
    final.chmod(0o440)
    shutil.rmtree(work)
    print(f"built {name}")
    print(f"select it with: vivado-runners images activate base {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
