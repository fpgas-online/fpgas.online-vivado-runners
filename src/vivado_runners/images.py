"""Versioned images and the *-current symlinks that select one.

Every image is kept under its version. Rolling back is repointing a symlink at
an earlier version; nothing here ever deletes an image.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

LINKS = {"base": "runner-base-current", "vivado": "vivado-current"}
PATTERNS = {"base": "runner-base-*.qcow2", "vivado": "vivado-*.squashfs"}


class ImageError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageVersions:
    base: Path
    vivado: Path


def _resolve_one(images_dir: Path, kind: str) -> Path:
    link = images_dir / LINKS[kind]
    if not link.is_symlink():
        raise ImageError(f"{link.name} is not a symlink in {images_dir}")
    target = link.resolve()
    if target.parent != images_dir.resolve():
        raise ImageError(f"{link.name} points outside {images_dir}: {target}")
    if not target.is_file():
        raise ImageError(f"{link.name} points at {target.name}, which does not exist")
    return images_dir / target.name


def resolve(images_dir: Path) -> ImageVersions:
    return ImageVersions(base=_resolve_one(images_dir, "base"), vivado=_resolve_one(images_dir, "vivado"))


def list_versions(images_dir: Path, kind: str) -> list[Path]:
    return sorted(p for p in images_dir.glob(PATTERNS[kind]) if not p.is_symlink())


def current(images_dir: Path, kind: str) -> Path | None:
    try:
        return _resolve_one(images_dir, kind)
    except ImageError:
        return None


def activate(images_dir: Path, kind: str, name: str) -> None:
    if images_dir / name not in list_versions(images_dir, kind):
        raise ImageError(f"no such {kind} image: {name}")
    tmp = images_dir / f".{LINKS[kind]}.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(name)
    os.replace(tmp, images_dir / LINKS[kind])


def next_base_name(images_dir: Path, date: str) -> str:
    pattern = re.compile(rf"^runner-base-{re.escape(date)}\.(\d+)\.qcow2$")
    builds = [int(m.group(1)) for p in images_dir.iterdir() if (m := pattern.match(p.name))]
    return f"runner-base-{date}.{max(builds, default=0) + 1}.qcow2"
