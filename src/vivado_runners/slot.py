"""One slot: boot a VM for one job, wait for it to end, remove every trace.

Outcome reasons: finished, no-job, register-timeout, wall-limit, start-error,
jit-error, low-disk, stopped, plus
  hypervisor-error  libvirt failed while the VM was running (counts as a failure)
  destroy-error     the VM could not be stopped: the slot halts at once and keeps the disks
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from . import images
from .config import Config
from .domain_xml import DomainSpec, render
from .hypervisor import HypervisorError

log = logging.getLogger("vivado_runners.slot")

FAILURES = frozenset({"no-job", "register-timeout", "start-error", "hypervisor-error"})
HALT_AFTER = 3
JIT_BACKOFF_START = 30
JIT_BACKOFF_MAX = 600
LOW_DISK_RETRY = 60
REGISTRATION_POLL = 30


@dataclass(frozen=True)
class Outcome:
    reason: str
    seconds: float
    runner: str | None
    base: str | None
    vivado: str | None


class Slot:
    def __init__(
        self,
        index: int,
        cfg: Config,
        github,
        hv,
        group_id: int,
        stop,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        new_id: Callable[[], str] | None = None,
        poll_seconds: float = 5.0,
    ):
        self.index = index
        self.cfg = cfg
        self.github = github
        self.hv = hv
        self.group_id = group_id
        self.stop = stop
        self.clock = clock
        self.sleep = sleep or (lambda seconds: stop.wait(seconds))
        self.new_id = new_id or (lambda: secrets.token_hex(4))
        self.poll_seconds = poll_seconds

    # -- status file, read by `vivado-runners status` --------------------------------

    def _status(self, state: str, **extra) -> None:
        self.cfg.status_dir.mkdir(parents=True, exist_ok=True)
        path = self.cfg.status_dir / f"slot-{self.index}.json"
        previous = json.loads(path.read_text()) if path.exists() else {}
        previous.update({"slot": self.index, "state": state, "since": datetime.now(UTC).isoformat(), **extra})
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(previous))
        tmp.replace(path)

    # -- one job ------------------------------------------------------------------

    def run_once(self) -> Outcome:
        cfg = self.cfg
        started = self.clock()

        def done(reason: str, runner: str | None = None, versions: images.ImageVersions | None = None) -> Outcome:
            outcome = Outcome(
                reason=reason,
                seconds=self.clock() - started,
                runner=runner,
                base=versions.base.name if versions else None,
                vivado=versions.vivado.name if versions else None,
            )
            self._status("idle", last_reason=reason, last_runner=runner)
            log.info("job %s", json.dumps({"slot": self.index, "host": cfg.host, **asdict(outcome)}))
            return outcome

        if self.hv.free_gib(cfg.state_dir) < cfg.min_free_gib:
            return done("low-disk")

        versions = images.resolve(cfg.images_dir)
        runner = f"{cfg.runner_prefix}{self.index}-{self.new_id()}"
        try:
            jit = self.github.create_jit(runner, self.group_id, list(cfg.slots.labels))
        except Exception:
            log.exception("slot %d: could not get a JIT runner config", self.index)
            return done("jit-error")

        domain = cfg.domain_name(self.index)
        slot_dir = cfg.slot_dir(self.index)
        reason = "start-error"
        try:
            disks = self.hv.prepare(
                slot_dir,
                versions.base,
                cfg.slots.scratch_gib,
                {"jitconfig": jit.encoded_jit_config, "proxy": cfg.proxy},
            )
            node = self._numa_node()
            cfg.console_dir.mkdir(parents=True, exist_ok=True)
            xml = render(
                DomainSpec(
                    name=domain,
                    vcpus=cfg.slots.vcpus,
                    memory_gib=cfg.slots.memory_gib,
                    mac=cfg.slot_mac(self.index),
                    ip=cfg.slot_ip(self.index),
                    network=cfg.network,
                    overlay=disks.overlay,
                    vivado=versions.vivado,
                    seed=disks.seed,
                    scratch=disks.scratch,
                    console_log=cfg.console_dir / f"slot-{self.index}.log",
                    cpuset=self.hv.node_cpulist(node) if node is not None else None,
                    numa_node=node,
                )
            )
            self.hv.start(domain, xml, slot_dir)
            reason = "hypervisor-error"
            self._status("running", runner=runner, domain=domain, base=versions.base.name, vivado=versions.vivado.name)
            reason = self._wait(domain, runner, started)
        except Exception:
            log.exception("slot %d: could not start %s", self.index, domain)
        finally:
            try:
                self.hv.destroy(domain)
            except HypervisorError:
                # The VM may still be running. Its disks stay where they are, and the slot stops.
                log.error("ALERT slot %d: could not destroy %s; keeping %s", self.index, domain, slot_dir)
                reason = "destroy-error"
            else:
                self.hv.wipe(slot_dir)
            reason = self._reap_runner(jit.id, reason)
        return done(reason, runner, versions)

    def _numa_node(self) -> int | None:
        """On a host with several NUMA nodes, keep each VM's CPUs and memory on one,
        spreading the slots across the nodes. The layout is read from the host."""
        if not self.cfg.slots.numa_pinning:
            return None
        nodes = self.hv.numa_nodes()
        return nodes[self.index % len(nodes)] if len(nodes) > 1 else None

    def _wait(self, domain: str, runner: str, started: float) -> str:
        wall = self.cfg.slots.wall_limit_minutes * 60
        register_limit = self.cfg.slots.register_timeout_minutes * 60
        registered = False
        next_check = 0.0
        while True:
            if self.stop.is_set():
                return "stopped"
            if not self.hv.is_running(domain):
                return "finished"
            elapsed = self.clock() - started
            if elapsed > wall:
                return "wall-limit"
            if not registered:
                if elapsed >= next_check:
                    next_check = elapsed + REGISTRATION_POLL
                    registered = self._is_online(runner)
                if not registered and elapsed > register_limit:
                    return "register-timeout"
            self.sleep(self.poll_seconds)

    def _is_online(self, runner: str) -> bool:
        try:
            return any(r.name == runner and r.status == "online" for r in self.github.list_runners())
        except Exception:
            log.exception("slot %d: could not list runners", self.index)
            return False

    def _reap_runner(self, runner_id: int, reason: str) -> str:
        """Delete the runner if GitHub still lists it. A VM that powered off while
        its runner is still listed never ran a job."""
        try:
            present = any(r.id == runner_id for r in self.github.list_runners())
            if present:
                self.github.delete_runner(runner_id)
        except Exception:
            log.exception("slot %d: could not remove runner %d", self.index, runner_id)
            return reason
        return "no-job" if present and reason == "finished" else reason

    # -- the loop ------------------------------------------------------------------

    def run_forever(self) -> str:
        failures = 0
        backoff = JIT_BACKOFF_START
        while not self.stop.is_set():
            outcome = self.run_once()
            if outcome.reason == "stopped":
                break
            if outcome.reason == "destroy-error":
                self._status("halted", last_reason=outcome.reason)
                log.error("ALERT slot %d halted: a VM could not be destroyed", self.index)
                return "halted"
            if outcome.reason == "jit-error":
                self.sleep(backoff)
                backoff = min(backoff * 2, JIT_BACKOFF_MAX)
                continue
            backoff = JIT_BACKOFF_START
            if outcome.reason == "low-disk":
                self.sleep(LOW_DISK_RETRY)
                continue
            if outcome.reason in FAILURES:
                failures += 1
                if failures >= HALT_AFTER:
                    self._status("halted", last_reason=outcome.reason)
                    log.error(
                        "ALERT slot %d halted after %d failures in a row (%s)", self.index, failures, outcome.reason
                    )
                    return "halted"
            else:
                failures = 0
        return "stopped"
