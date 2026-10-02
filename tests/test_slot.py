import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fakes import FakeClock, FakeGitHub, FakeHypervisor, FakeStop
from vivado_runners import slot as slot_mod
from vivado_runners.config import Config, GithubConfig, SlotsConfig
from vivado_runners.slot import Slot


@pytest.fixture
def env(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "runner-base-2026-10-02.1.qcow2").write_bytes(b"x")
    (images / "vivado-2025.2-2026-10-02.squashfs").write_bytes(b"x")
    (images / "runner-base-current").symlink_to("runner-base-2026-10-02.1.qcow2")
    (images / "vivado-current").symlink_to("vivado-2025.2-2026-10-02.squashfs")
    cfg = Config(
        host="alpha",
        state_dir=tmp_path,
        min_free_gib=100,
        network="vivado-runners",
        proxy="http://192.168.76.1:3128",
        github=GithubConfig("fpgas-online", 1, 2, Path("/k"), "vivado"),
        slots=SlotsConfig(
            count=2, vcpus=8, memory_gib=24, scratch_gib=60, wall_limit_minutes=120, labels=("self-hosted", "x")
        ),
    )
    clock = FakeClock()
    gh = FakeGitHub()
    gh.clock = clock
    hv = FakeHypervisor(clock, gh)
    stop = FakeStop()

    def make(index=0, cfg=cfg):
        return Slot(
            index, cfg, gh, hv, group_id=7, stop=stop, clock=clock, sleep=clock.sleep, new_id=lambda: "deadbeef"
        )

    return type("Env", (), {"cfg": cfg, "clock": clock, "gh": gh, "hv": hv, "stop": stop, "make": staticmethod(make)})


def test_a_normal_job(env):
    out = env.make().run_once()
    assert out.reason == "finished"
    assert out.runner == "alpha-slot0-deadbeef"
    assert (out.base, out.vivado) == ("runner-base-2026-10-02.1.qcow2", "vivado-2025.2-2026-10-02.squashfs")
    assert 600 <= out.seconds < 610
    assert env.gh.deleted == [], "GitHub already removed the runner; nothing to delete"
    kinds = [e[0] for e in env.hv.events]
    assert kinds == ["prepare", "start", "destroy", "wipe"]
    assert env.hv.events[0] == (
        "prepare",
        "slot-0",
        "runner-base-2026-10-02.1.qcow2",
        60,
        {"jitconfig": "jit-100", "proxy": "http://192.168.76.1:3128"},
    )


def test_the_domain_xml_uses_the_slot_addressing(env):
    env.make(index=1).run_once()
    assert "<name>vr-alpha-1</name>" in env.hv.last_xml
    assert 'address="52:54:00:76:00:01"' in env.hv.last_xml
    assert 'value="192.168.76.11"' in env.hv.last_xml
    assert "cpuset" not in env.hv.last_xml


def test_a_single_node_host_is_not_pinned(env):
    env.hv.nodes = [0]
    env.make(index=1).run_once()
    assert "cpuset" not in env.hv.last_xml
    assert "numatune" not in env.hv.last_xml


@pytest.mark.parametrize(("index", "node", "cpus"), [(0, 0, "0-3,8-11"), (1, 1, "4-7,12-15"), (2, 0, "0-3,8-11")])
def test_slots_are_spread_over_the_hosts_numa_nodes(env, index, node, cpus):
    env.hv.nodes = [0, 1]
    cfg = replace(env.cfg, slots=replace(env.cfg.slots, count=3))
    env.make(index=index, cfg=cfg).run_once()
    assert f'cpuset="{cpus}"' in env.hv.last_xml
    assert f'nodeset="{node}"' in env.hv.last_xml


def test_numa_pinning_can_be_switched_off(env):
    env.hv.nodes = [0, 1]
    cfg = replace(env.cfg, slots=replace(env.cfg.slots, numa_pinning=False))
    env.make(cfg=cfg).run_once()
    assert "cpuset" not in env.hv.last_xml


def test_vm_that_powers_off_without_running_a_job(env):
    env.hv.runs_job = False
    env.hv.run_seconds = 40.0
    out = env.make().run_once()
    assert out.reason == "no-job"
    assert env.gh.deleted == [100]


def test_runner_never_registers(env):
    env.gh.online_after = None
    env.hv.run_seconds = None
    out = env.make().run_once()
    assert out.reason == "register-timeout"
    assert 300 < out.seconds < 340
    assert ("destroy", "vr-alpha-0") in env.hv.events
    assert env.gh.deleted == [100]


def test_wall_limit_kills_a_job_that_never_ends(env):
    env.hv.run_seconds = None
    out = env.make().run_once()
    assert out.reason == "wall-limit"
    assert 7200 < out.seconds < 7210
    assert ("destroy", "vr-alpha-0") in env.hv.events


def test_registration_is_not_polled_once_seen(env):
    calls = []
    original = env.gh.list_runners
    env.gh.list_runners = lambda: calls.append(env.clock()) or original()
    env.make().run_once()
    assert len(calls) == 2, "once while waiting to register, once after the VM stopped"


def test_start_failure_still_cleans_up(env):
    env.hv.start_error = RuntimeError("virsh create failed")
    out = env.make().run_once()
    assert out.reason == "start-error"
    assert [e[0] for e in env.hv.events] == ["prepare", "destroy", "wipe"]
    assert env.gh.deleted == [100]


def test_jit_failure_starts_nothing(env):
    env.gh.jit_errors = 1
    out = env.make().run_once()
    assert out.reason == "jit-error"
    assert env.hv.events == []


def test_low_disk_starts_nothing_and_asks_github_for_nothing(env):
    env.hv.free = 50.0
    out = env.make().run_once()
    assert out.reason == "low-disk"
    assert env.gh.created == []
    assert env.hv.events == []


def test_stop_destroys_the_running_vm(env):
    env.hv.run_seconds = None
    original = env.clock.sleep

    def sleep(seconds):
        original(seconds)
        if env.clock.now > 60:
            env.stop.flag = True

    s = Slot(0, env.cfg, env.gh, env.hv, group_id=7, stop=env.stop, clock=env.clock, sleep=sleep, new_id=lambda: "ab")
    out = s.run_once()
    assert out.reason == "stopped"
    assert ("destroy", "vr-alpha-0") in env.hv.events


def test_status_file_tracks_the_slot(env):
    env.make().run_once()
    status = json.loads((env.cfg.status_dir / "slot-0.json").read_text())
    assert status["state"] == "idle"
    assert status["last_reason"] == "finished"
    assert status["last_runner"] == "alpha-slot0-deadbeef"


def test_run_forever_halts_after_three_failures_in_a_row(env):
    env.hv.runs_job = False
    env.hv.run_seconds = 40.0
    assert env.make().run_forever() == "halted"
    assert len(env.gh.created) == 3
    assert json.loads((env.cfg.status_dir / "slot-0.json").read_text())["state"] == "halted"


def test_a_success_resets_the_failure_count(env):
    outcomes = iter(["no-job", "no-job", "finished", "no-job", "no-job", "no-job"])
    s = env.make()
    s.run_once = lambda: slot_mod.Outcome(next(outcomes), 1.0, "r", "b", "v")
    assert s.run_forever() == "halted"
    assert list(outcomes) == []


def test_jit_errors_back_off_and_do_not_halt_the_slot(env):
    outcomes = iter(["jit-error"] * 6 + ["finished"])

    def once():
        reason = next(outcomes)
        if reason == "finished":
            env.stop.flag = True
        return slot_mod.Outcome(reason, 0.0, None, None, None)

    s = env.make()
    s.run_once = once
    assert s.run_forever() == "stopped"
    assert env.clock.sleeps == [30, 60, 120, 240, 480, 600]


def test_low_disk_retries_after_a_minute(env):
    outcomes = iter(["low-disk", "low-disk", "finished"])

    def once():
        reason = next(outcomes)
        if reason == "finished":
            env.stop.flag = True
        return slot_mod.Outcome(reason, 0.0, None, None, None)

    s = env.make()
    s.run_once = once
    s.run_forever()
    assert env.clock.sleeps == [60, 60]


def test_a_vm_that_cannot_be_destroyed_keeps_its_disks(env):
    env.hv.destroy_error = True
    out = env.make().run_once()
    assert out.reason == "destroy-error"
    assert ("wipe", "slot-0") not in env.hv.events


def test_a_vm_that_cannot_be_destroyed_halts_the_slot_at_once(env):
    env.hv.destroy_error = True
    assert env.make().run_forever() == "halted"
    assert len(env.gh.created) == 1
    assert json.loads((env.cfg.status_dir / "slot-0.json").read_text())["state"] == "halted"


def test_losing_libvirt_while_waiting_is_a_failure_and_still_cleans_up(env):
    original = env.hv.start

    def start_then_fail(name, xml, slot_dir):
        original(name, xml, slot_dir)
        env.hv.running_error = True

    env.hv.start = start_then_fail
    out = env.make().run_once()
    assert out.reason == "hypervisor-error"
    assert [e[0] for e in env.hv.events] == ["prepare", "start", "destroy", "wipe"]
