import threading
from pathlib import Path

import pytest

from tests.fakes import FakeClock, FakeGitHub, FakeHypervisor
from vivado_runners import controller
from vivado_runners.config import Config, ConfigError, GithubConfig, SlotsConfig
from vivado_runners.github import GitHubError, Runner
from vivado_runners.hypervisor import HypervisorError


def make_cfg(tmp_path, count=2):
    return Config(
        host="alpha",
        state_dir=tmp_path,
        min_free_gib=100,
        network="vivado-runners",
        proxy="http://192.168.76.1:3128",
        github=GithubConfig("fpgas-online", 1, 2, Path("/k"), "vivado"),
        slots=SlotsConfig(count=count, vcpus=8, memory_gib=24, scratch_gib=60, wall_limit_minutes=120, labels=("x",)),
    )


def test_startup_cleanup_removes_only_this_hosts_leftovers(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = FakeClock()
    gh = FakeGitHub()
    hv = FakeHypervisor(clock, gh)
    hv.domains = {"vr-alpha-0": 0.0, "vr-beta-0": 0.0, "docker": 0.0}
    gh.runners = {
        1: Runner(1, "alpha-slot0-aaaa", "offline", False),
        2: Runner(2, "beta-slot0-bbbb", "online", False),
        3: Runner(3, "alpha-slot1-cccc", "online", True),
    }
    gh._born = {1: 0.0, 2: 0.0, 3: 0.0}
    controller.startup_cleanup(cfg, gh, hv)
    assert ("destroy", "vr-alpha-0") in hv.events
    assert ("destroy", "vr-beta-0") not in hv.events
    assert [e for e in hv.events if e[0] == "wipe"] == [("wipe", f"slot-{i}") for i in range(8)]
    assert sorted(gh.deleted) == [1, 3]


def test_startup_cleanup_survives_a_runner_github_will_not_delete(tmp_path):
    cfg = make_cfg(tmp_path)
    gh = FakeGitHub()
    hv = FakeHypervisor(FakeClock(), gh)
    gh.runners = {1: Runner(1, "alpha-slot0-aaaa", "online", True)}
    gh._born = {1: 0.0}

    def refuse(runner_id):
        raise GitHubError(422, "runner is busy")

    gh.delete_runner = refuse
    controller.startup_cleanup(cfg, gh, hv)


def test_startup_cleanup_wipes_nothing_when_a_leftover_vm_cannot_be_destroyed(tmp_path):
    cfg = make_cfg(tmp_path)
    gh = FakeGitHub()
    hv = FakeHypervisor(FakeClock(), gh)
    hv.domains = {"vr-alpha-0": 0.0}
    hv.destroy_error = True
    with pytest.raises(HypervisorError):
        controller.startup_cleanup(cfg, gh, hv)
    assert not any(e[0] == "wipe" for e in hv.events)


def test_run_returns_1_when_every_slot_halts(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    gh = FakeGitHub()
    gh.runner_group_id = lambda name: 7
    hv = FakeHypervisor(FakeClock(), gh)

    class Halting:
        def __init__(self, index, *args, **kwargs):
            self.index = index

        def run_forever(self):
            return "halted"

    monkeypatch.setattr(controller, "Slot", Halting)
    assert controller.run(cfg, github=gh, hv=hv, stop=threading.Event()) == 1


def test_run_returns_0_when_stopped(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    gh = FakeGitHub()
    gh.runner_group_id = lambda name: 7
    hv = FakeHypervisor(FakeClock(), gh)
    stop = threading.Event()
    started = []

    class Waiting:
        def __init__(self, index, cfg, github, hv, group_id, stop, **kwargs):
            self.stop = stop
            started.append((index, group_id))

        def run_forever(self):
            self.stop.wait(5)
            return "stopped"

    monkeypatch.setattr(controller, "Slot", Waiting)
    threading.Timer(0.2, stop.set).start()
    assert controller.run(cfg, github=gh, hv=hv, stop=stop) == 0
    assert sorted(started) == [(0, 7), (1, 7)]


def test_capacity_is_checked_against_the_host_it_runs_on(tmp_path):
    cfg = make_cfg(tmp_path, count=4)  # 4 slots of 8 vCPUs and 24 GiB
    hv = FakeHypervisor(FakeClock(), FakeGitHub())
    hv.cpus, hv.memory_gib = 32, 128.0
    controller.check_capacity(cfg, hv)
    hv.cpus = 12
    with pytest.raises(ConfigError, match="need 32 CPUs; this host has 12"):
        controller.check_capacity(cfg, hv)
    hv.cpus, hv.memory_gib = 32, 125.0
    with pytest.raises(ConfigError, match=r"need 96 GiB; the limit is 94 GiB \(75% of this host's 125 GiB\)"):
        controller.check_capacity(cfg, hv)


def test_run_refuses_to_start_on_a_host_that_is_too_small(tmp_path):
    cfg = make_cfg(tmp_path, count=4)
    gh = FakeGitHub()
    hv = FakeHypervisor(FakeClock(), gh)
    hv.cpus = 8
    with pytest.raises(ConfigError):
        controller.run(cfg, github=gh, hv=hv, stop=threading.Event())
    assert hv.events == [], "nothing was cleaned up or started"
