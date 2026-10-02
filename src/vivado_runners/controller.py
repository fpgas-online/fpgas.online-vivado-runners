"""Start-up cleanup, one thread per slot, and shutdown on a signal."""

from __future__ import annotations

import logging
import signal
import threading

from .config import MAX_SLOTS, Config, ConfigError
from .github import GitHubClient, GitHubError
from .hypervisor import VirshHypervisor
from .slot import Slot

log = logging.getLogger("vivado_runners.controller")


MEMORY_SHARE = 0.75


def check_capacity(cfg: Config, hv) -> None:
    """Refuse a configuration this host cannot carry, whichever host it is."""
    vcpus = cfg.slots.count * cfg.slots.vcpus
    if vcpus > hv.host_cpus():
        raise ConfigError(
            f"{cfg.slots.count} slots of {cfg.slots.vcpus} vCPUs need {vcpus} CPUs; this host has {hv.host_cpus()}"
        )
    memory = cfg.slots.count * cfg.slots.memory_gib
    limit = hv.host_memory_gib() * MEMORY_SHARE
    if memory > limit:
        raise ConfigError(
            f"{cfg.slots.count} slots of {cfg.slots.memory_gib} GiB need {memory} GiB; "
            f"the limit is {limit:.0f} GiB ({MEMORY_SHARE:.0%} of this host's {hv.host_memory_gib():.0f} GiB)"
        )


def startup_cleanup(cfg: Config, github, hv) -> None:
    """Nothing from a previous run survives: its VMs, its disks, its runners."""
    for name in hv.list_domains(cfg.domain_prefix):
        log.warning("destroying leftover domain %s", name)
        hv.destroy(name)
    for index in range(MAX_SLOTS):
        hv.wipe(cfg.slot_dir(index))
    for runner in github.list_runners():
        if not runner.name.startswith(cfg.runner_prefix):
            continue
        log.warning("removing leftover runner %s (%s)", runner.name, runner.status)
        try:
            github.delete_runner(runner.id)
        except GitHubError as error:
            log.warning("could not remove runner %s: %s", runner.name, error)


def run(cfg: Config, github=None, hv=None, stop: threading.Event | None = None, install_signals: bool = False) -> int:
    hv = hv or VirshHypervisor()
    check_capacity(cfg, hv)
    github = github or GitHubClient(
        cfg.github.org, cfg.github.app_id, cfg.github.installation_id, cfg.github.key_file.read_text()
    )
    stop = stop or threading.Event()

    startup_cleanup(cfg, github, hv)
    group_id = github.runner_group_id(cfg.github.runner_group)

    results: dict[int, str] = {}

    def work(index: int) -> None:
        results[index] = Slot(index, cfg, github, hv, group_id, stop).run_forever()

    if install_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())

    threads = [threading.Thread(target=work, args=(i,), name=f"slot-{i}") for i in range(cfg.slots.count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        while thread.is_alive():
            thread.join(timeout=1.0)

    if all(reason == "halted" for reason in results.values()):
        log.error("ALERT every slot halted")
        return 1
    return 0
