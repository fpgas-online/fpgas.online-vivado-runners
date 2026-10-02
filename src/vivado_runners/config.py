"""Controller configuration, loaded from TOML."""

from __future__ import annotations

import re
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path

SUBNET = "192.168.76"
BRIDGE = "vrbr0"
MAX_SLOTS = 8
HOST_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class GithubConfig:
    org: str
    app_id: int
    installation_id: int
    key_file: Path
    runner_group: str


@dataclass(frozen=True)
class SlotsConfig:
    count: int
    vcpus: int
    memory_gib: int
    scratch_gib: int
    wall_limit_minutes: int
    labels: tuple[str, ...]
    numa_pinning: bool = True
    register_timeout_minutes: int = 5


@dataclass(frozen=True)
class Config:
    host: str
    state_dir: Path
    min_free_gib: int
    network: str
    proxy: str
    github: GithubConfig
    slots: SlotsConfig

    @property
    def images_dir(self) -> Path:
        return self.state_dir / "images"

    @property
    def status_dir(self) -> Path:
        return self.state_dir / "status"

    @property
    def console_dir(self) -> Path:
        return self.state_dir / "console"

    @property
    def runner_prefix(self) -> str:
        return f"{self.host}-slot"

    @property
    def domain_prefix(self) -> str:
        return f"vr-{self.host}-"

    def slot_dir(self, index: int) -> Path:
        return self.state_dir / f"slot-{index}"

    def slot_mac(self, index: int) -> str:
        return f"52:54:00:76:00:{index:02x}"

    def slot_ip(self, index: int) -> str:
        return f"{SUBNET}.{10 + index}"

    def domain_name(self, index: int) -> str:
        return f"{self.domain_prefix}{index}"


def _need(table: dict, section: str, key: str):
    if key not in table:
        raise ConfigError(f"missing {section}.{key}")
    return table[key]


def load(path: Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    gh = raw.get("github", {})
    sl = raw.get("slots", {})

    github = GithubConfig(
        org=_need(gh, "github", "org"),
        app_id=int(_need(gh, "github", "app_id")),
        installation_id=int(_need(gh, "github", "installation_id")),
        key_file=Path(_need(gh, "github", "key_file")),
        runner_group=_need(gh, "github", "runner_group"),
    )
    slots = SlotsConfig(
        count=int(_need(sl, "slots", "count")),
        vcpus=int(_need(sl, "slots", "vcpus")),
        memory_gib=int(_need(sl, "slots", "memory_gib")),
        scratch_gib=int(_need(sl, "slots", "scratch_gib")),
        wall_limit_minutes=int(_need(sl, "slots", "wall_limit_minutes")),
        labels=tuple(_need(sl, "slots", "labels")),
        numa_pinning=bool(sl.get("numa_pinning", True)),
        register_timeout_minutes=int(sl.get("register_timeout_minutes", 5)),
    )
    if not 1 <= slots.count <= MAX_SLOTS:
        raise ConfigError(f"slots.count must be 1..{MAX_SLOTS}")
    if not slots.labels:
        raise ConfigError("slots.labels must not be empty")
    # The name is part of every runner and VM name, and start-up cleanup removes
    # whatever carries it, so it has to be this host's alone.
    host = raw.get("host") or socket.gethostname().split(".")[0].lower()
    if not HOST_NAME.match(host):
        raise ConfigError(f"host must be lower-case letters, digits and hyphens, at most 31 characters: {host!r}")

    return Config(
        host=host,
        state_dir=Path(raw.get("state_dir", "/var/lib/vivado-runners")),
        min_free_gib=int(raw.get("min_free_gib", 100)),
        network=raw.get("network", "vivado-runners"),
        proxy=raw.get("proxy", f"http://{SUBNET}.1:3128"),
        github=github,
        slots=slots,
    )
