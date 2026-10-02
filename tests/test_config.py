from pathlib import Path

import pytest

from vivado_runners import config

GOOD = """
host = "alpha"

[github]
org = "fpgas-online"
app_id = 123
installation_id = 456
key_file = "/etc/vivado-runners/app.pem"
runner_group = "vivado"

[slots]
count = 4
vcpus = 8
memory_gib = 24
scratch_gib = 60
wall_limit_minutes = 120
labels = ["self-hosted", "linux", "x64", "vivado-2025.2"]
"""


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_loads_a_complete_file(tmp_path):
    cfg = config.load(write(tmp_path, GOOD))
    assert cfg.host == "alpha"
    assert cfg.github.app_id == 123
    assert cfg.slots.labels == ("self-hosted", "linux", "x64", "vivado-2025.2")
    assert cfg.slots.numa_pinning is True
    assert cfg.slots.register_timeout_minutes == 5
    assert cfg.state_dir == Path("/var/lib/vivado-runners")
    assert cfg.proxy == "http://192.168.76.1:3128"
    assert cfg.network == "vivado-runners"
    assert cfg.min_free_gib == 100


def test_slot_addressing(tmp_path):
    cfg = config.load(write(tmp_path, GOOD))
    assert cfg.slot_mac(0) == "52:54:00:76:00:00"
    assert cfg.slot_mac(3) == "52:54:00:76:00:03"
    assert cfg.slot_ip(3) == "192.168.76.13"
    assert cfg.domain_name(3) == "vr-alpha-3"
    assert cfg.domain_prefix == "vr-alpha-"
    assert cfg.runner_prefix == "alpha-slot"
    assert cfg.slot_dir(3) == Path("/var/lib/vivado-runners/slot-3")
    assert cfg.images_dir == Path("/var/lib/vivado-runners/images")


def test_host_defaults_to_short_hostname(tmp_path, monkeypatch):
    monkeypatch.setattr(config.socket, "gethostname", lambda: "beta.example.org")
    cfg = config.load(write(tmp_path, GOOD.replace('host = "alpha"\n', "")))
    assert cfg.host == "beta"


def test_numa_pinning_can_be_switched_off(tmp_path):
    cfg = config.load(write(tmp_path, GOOD + "numa_pinning = false\n"))
    assert cfg.slots.numa_pinning is False


def test_nothing_about_the_host_is_required(tmp_path, monkeypatch):
    """The same file works on any host: its name is the only host-specific value, and it has a default."""
    monkeypatch.setattr(config.socket, "gethostname", lambda: "Gamma.example.org")
    cfg = config.load(write(tmp_path, GOOD.replace('host = "alpha"\n', "")))
    assert cfg.host == "gamma"
    assert cfg.domain_name(0) == "vr-gamma-0"
    assert cfg.runner_prefix == "gamma-slot"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("count = 4", "count = 9", "slots.count must be 1..8"),
        ("count = 4", "count = 0", "slots.count must be 1..8"),
        ('host = "alpha"', 'host = "Alpha Host"', "host must be lower-case letters, digits and hyphens"),
        ('labels = ["self-hosted", "linux", "x64", "vivado-2025.2"]', "labels = []", "slots.labels must not be empty"),
        ("app_id = 123", "", "missing github.app_id"),
    ],
)
def test_rejects_bad_values(tmp_path, old, new, message):
    assert old in GOOD
    with pytest.raises(config.ConfigError, match=message):
        config.load(write(tmp_path, GOOD.replace(old, new)))
