#!/usr/bin/env python3
"""Pack a Vivado install into a versioned, read-only squashfs disk.

    uv run python image/build_vivado_disk.py --source /opt/Xilinx/2025.2 \\
        --images-dir /var/lib/vivado-runners/images

The result stays on the runner hosts. Never upload it: AMD's licence allows
installing Vivado, not redistributing it. It never touches vivado-current:
select the new disk with `vivado-runners images activate vivado <name>`.

The disk is attached, readable, to every job's VM. So the build refuses a
source tree that holds a licence file tied to a machine (a node-locked or
server licence): every job could read it. The generic licences AMD ships
inside the product (HOSTID=ANY) are fine.

Scans for licence-like files with names matching *.lic*, *.dat, license*,
*.slic (up to 1 MiB each) and judges only those that look like FlexLM licences.
"""

import argparse
import datetime
import fnmatch
import pathlib
import re
import subprocess
import sys

# A FlexLM licence file has lines that start with one of these keywords.
LICENCE_LINE = re.compile(
    r"^\s*(INCREMENT|FEATURE|PACKAGE|UPGRADE|SERVER|VENDOR|DAEMON|USE_SERVER)\b",
    re.I | re.M,
)
SERVER = re.compile(r"^\s*(SERVER|USE_SERVER)\b", re.I | re.M)
# HOSTID, optional spaces, '=', optional spaces and backslash-newline continuations, then the value.
HOSTID = re.compile(r"\bHOSTID\s*=\s*(?:\\\r?\n\s*)*(\"[^\"]*\"|[^\s\\]*)", re.I)
# Names a licence file is usually given; anything bigger than this is not a licence.
CANDIDATE_NAMES = ("*.lic*", "*.dat", "license*", "*.slic")
CANDIDATE_MAX_BYTES = 1 << 20


def _text(data: bytes) -> str:
    """Licence text from bytes in any encoding an editor might have saved it in."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data[:200]:
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return data.replace(b"\x00", b"").decode("latin-1")
    return data.decode("latin-1")


def is_machine_tied(data: bytes) -> bool:
    """True for FlexLM licence text locked to a host ID or pointing at a licence server.
    A HOSTID with no value, or any value other than ANY or DEMO, counts as locked."""
    text = _text(data)
    if not LICENCE_LINE.search(text):
        return False
    if SERVER.search(text):
        return True
    return any(value.strip('"').upper() not in ("ANY", "DEMO") for value in HOSTID.findall(text))


def _is_candidate(path: pathlib.Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in CANDIDATE_NAMES)


def machine_tied_licences(source: pathlib.Path) -> list[pathlib.Path]:
    found = []
    for path in sorted(source.rglob("*")):
        if not _is_candidate(path) or not path.is_file() or path.is_symlink():
            continue
        if path.stat().st_size <= CANDIDATE_MAX_BYTES and is_machine_tied(path.read_bytes()):
            found.append(path)
    return found


def output_name(source: pathlib.Path, date: str) -> str:
    return f"vivado-{source.name}-{date}.squashfs"


def command(source: pathlib.Path, dest: pathlib.Path, excludes: list[str]) -> list[str]:
    cmd = [
        "mksquashfs", str(source), str(dest),
        "-keep-as-directory", "-all-root", "-no-xattrs", "-noappend",
        "-comp", "zstd", "-Xcompression-level", "15", "-b", "1M",
    ]  # fmt: skip
    if excludes:
        cmd += ["-e", *[f"{source.name}/{rel}" for rel in excludes]]
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=pathlib.Path, required=True, help="e.g. /opt/Xilinx/2025.2")
    parser.add_argument("--images-dir", type=pathlib.Path, required=True)
    parser.add_argument("--exclude", action="append", default=[], help="path relative to --source; repeatable")
    args = parser.parse_args()

    if not (args.source / "Vivado" / "settings64.sh").is_file():
        raise SystemExit(f"{args.source} is not a Vivado install: Vivado/settings64.sh is missing")
    tied = machine_tied_licences(args.source)
    if tied:
        listed = "\n  ".join(str(path) for path in tied)
        raise SystemExit(
            "refusing to build: these licence files are tied to a machine, "
            f"and every job VM could read them:\n  {listed}\n"
            "Move them out of the Vivado tree. Licensed Vivado versions are not supported yet (see the spec)."
        )
    dest = args.images_dir / output_name(args.source, datetime.date.today().isoformat())
    if dest.exists():
        raise SystemExit(f"{dest} already exists; images are never overwritten")

    cmd = command(args.source, dest, args.exclude)
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    dest.chmod(0o440)
    print(f"built {dest.name} ({dest.stat().st_size / 2**30:.1f} GiB)")
    print(f"select it with: vivado-runners images activate vivado {dest.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
