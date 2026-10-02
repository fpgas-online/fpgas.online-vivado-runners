"""Test doubles for GitHub, the hypervisor and time."""

from __future__ import annotations

from pathlib import Path

from vivado_runners.github import GitHubError, JitRunner, Runner
from vivado_runners.hypervisor import HypervisorError, SlotDisks


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeStop:
    def __init__(self):
        self.flag = False

    def is_set(self) -> bool:
        return self.flag


class FakeGitHub:
    def __init__(self):
        self.runners: dict[int, Runner] = {}
        self.next_id = 100
        self.jit_errors = 0  # how many create_jit calls fail before one succeeds
        self.online_after: float | None = 0.0  # None: the runner never comes online
        self.clock = None
        self.created: list[str] = []
        self.deleted: list[int] = []
        self._born: dict[int, float] = {}

    def create_jit(self, name, group_id, labels):
        if self.jit_errors:
            self.jit_errors -= 1
            raise GitHubError(503, "unavailable")
        runner_id = self.next_id
        self.next_id += 1
        self.runners[runner_id] = Runner(id=runner_id, name=name, status="offline", busy=False)
        self._born[runner_id] = self.clock() if self.clock else 0.0
        self.created.append(name)
        return JitRunner(id=runner_id, encoded_jit_config=f"jit-{runner_id}")

    def list_runners(self):
        now = self.clock() if self.clock else 0.0
        out = []
        for r in self.runners.values():
            online = self.online_after is not None and now - self._born[r.id] >= self.online_after
            out.append(Runner(id=r.id, name=r.name, status="online" if online else "offline", busy=False))
        return out

    def delete_runner(self, runner_id):
        self.deleted.append(runner_id)
        self.runners.pop(runner_id, None)

    def job_done(self, runner_id):
        """GitHub removes an ephemeral runner once it has run its job."""
        self.runners.pop(runner_id, None)


class FakeHypervisor:
    def __init__(self, clock: FakeClock, github: FakeGitHub):
        self.clock = clock
        self.github = github
        self.run_seconds: float | None = 600.0  # None: the VM never powers off
        self.runs_job = True
        self.free = 1000.0
        self.nodes = [0]
        self.cpus = 64
        self.memory_gib = 256.0
        self.start_error: Exception | None = None
        self.destroy_error = False
        self.running_error = False
        self.domains: dict[str, float] = {}  # name -> start time
        self.events: list[tuple] = []

    def list_domains(self, prefix):
        return [n for n in self.domains if n.startswith(prefix)]

    def prepare(self, slot_dir, base, scratch_gib, seed_files):
        self.events.append(("prepare", slot_dir.name, base.name, scratch_gib, dict(seed_files)))
        return SlotDisks(slot_dir / "overlay.qcow2", slot_dir / "scratch.qcow2", slot_dir / "seed.iso")

    def start(self, name, xml, slot_dir):
        if self.start_error:
            raise self.start_error
        self.events.append(("start", name))
        self.domains[name] = self.clock()
        self.last_xml = xml

    def is_running(self, name):
        if self.running_error:
            raise HypervisorError("virsh domstate: failed to connect")
        if name not in self.domains:
            return False
        if self.run_seconds is not None and self.clock() - self.domains[name] >= self.run_seconds:
            del self.domains[name]
            if self.runs_job:
                newest = max(self.github.runners, default=None)
                if newest is not None:
                    self.github.job_done(newest)
            return False
        return True

    def destroy(self, name):
        self.events.append(("destroy", name))
        if self.destroy_error:
            raise HypervisorError("virsh destroy: failed to connect")
        self.domains.pop(name, None)

    def wipe(self, slot_dir: Path):
        self.events.append(("wipe", slot_dir.name))

    def free_gib(self, path):
        return self.free

    def numa_nodes(self):
        return self.nodes

    def host_cpus(self):
        return self.cpus

    def host_memory_gib(self):
        return self.memory_gib

    def node_cpulist(self, node):
        return {0: "0-3,8-11", 1: "4-7,12-15"}[node]
