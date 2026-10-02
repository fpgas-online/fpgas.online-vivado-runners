import json

import pytest

from vivado_runners import cli

CONFIG = """
host = "alpha"
state_dir = "{state}"

[github]
org = "fpgas-online"
app_id = 1
installation_id = 2
key_file = "/k"
runner_group = "vivado"

[slots]
count = 2
vcpus = 8
memory_gib = 24
scratch_gib = 60
wall_limit_minutes = 120
labels = ["x"]
"""


@pytest.fixture
def cfg_path(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    for name in (
        "runner-base-2026-09-25.1.qcow2",
        "runner-base-2026-10-02.1.qcow2",
        "vivado-2025.2-2026-10-02.squashfs",
    ):
        (images / name).write_bytes(b"x")
    (images / "runner-base-current").symlink_to("runner-base-2026-10-02.1.qcow2")
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(state=tmp_path))
    return path


def test_images_list_marks_the_current_version(cfg_path, capsys):
    assert cli.main(["images", "list", "--config", str(cfg_path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "base      runner-base-2026-09-25.1.qcow2",
        "base    * runner-base-2026-10-02.1.qcow2",
        "vivado    vivado-2025.2-2026-10-02.squashfs",
    ]


def test_images_activate_rolls_back(cfg_path, capsys):
    args = ["images", "activate", "base", "runner-base-2026-09-25.1.qcow2", "--config", str(cfg_path)]
    assert cli.main(args) == 0
    assert (cfg_path.parent / "images" / "runner-base-current").readlink().name == "runner-base-2026-09-25.1.qcow2"
    assert "runner-base-current -> runner-base-2026-09-25.1.qcow2" in capsys.readouterr().out


def test_images_activate_reports_an_unknown_version(cfg_path, capsys):
    assert cli.main(["images", "activate", "base", "nope.qcow2", "--config", str(cfg_path)]) == 2
    assert "no such base image: nope.qcow2" in capsys.readouterr().err


def test_status_prints_one_line_per_slot(cfg_path, capsys):
    status = cfg_path.parent / "status"
    status.mkdir()
    (status / "slot-0.json").write_text(
        json.dumps({"slot": 0, "state": "running", "since": "2026-10-02T01:00:00+00:00", "runner": "alpha-slot0-ab"})
    )
    assert cli.main(["status", "--config", str(cfg_path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["SLOT", "STATE", "SINCE", "RUNNER", "LAST"]
    assert out[1].split() == ["0", "running", "2026-10-02T01:00:00+00:00", "alpha-slot0-ab", "-"]
    assert out[2].split() == ["1", "unknown", "-", "-", "-"]


def test_run_hands_the_config_to_the_controller(cfg_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.controller, "run", lambda cfg, install_signals: seen.setdefault("host", cfg.host) and 0)
    assert cli.main(["run", "--config", str(cfg_path)]) == 0
    assert seen == {"host": "alpha"}


def test_a_bad_config_is_exit_code_2(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text("[github]\n")
    assert cli.main(["status", "--config", str(path)]) == 2
    assert "missing github.org" in capsys.readouterr().err


def test_sandbox_targets_prints_the_hosts_own_addresses(monkeypatch, capsys):
    class Host:
        def host_addresses(self, exclude):
            assert exclude == ("vrbr0",)
            return ["203.0.113.7", "198.51.100.1"]

    monkeypatch.setattr(cli, "VirshHypervisor", Host)
    assert cli.main(["sandbox-targets"]) == 0
    assert capsys.readouterr().out == "203.0.113.7:22 198.51.100.1:22\n"


def test_a_host_too_small_for_the_config_is_exit_code_2(cfg_path, monkeypatch, capsys):
    def too_small(cfg, install_signals):
        raise cli.config.ConfigError("2 slots of 8 vCPUs need 16 CPUs; this host has 4")

    monkeypatch.setattr(cli.controller, "run", too_small)
    assert cli.main(["run", "--config", str(cfg_path)]) == 2
    assert "this host has 4" in capsys.readouterr().err
