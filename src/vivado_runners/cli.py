"""vivado-runners: run the controller, inspect slots, select images."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import config, controller, images
from .github import GitHubClient
from .hypervisor import VirshHypervisor

DEFAULT_CONFIG = "/etc/vivado-runners/config.toml"


def _status(cfg: config.Config) -> int:
    print(f"{'SLOT':<5}{'STATE':<10}{'SINCE':<34}{'RUNNER':<34}LAST")
    for index in range(cfg.slots.count):
        path = cfg.status_dir / f"slot-{index}.json"
        s = json.loads(path.read_text()) if path.exists() else {}
        runner = s.get("runner") if s.get("state") == "running" else None
        print(
            f"{index:<5}{s.get('state', 'unknown'):<10}{s.get('since', '-'):<34}"
            f"{runner or '-':<34}{s.get('last_reason') or '-'}"
        )
    return 0


def _images_list(cfg: config.Config) -> int:
    for kind in ("base", "vivado"):
        current = images.current(cfg.images_dir, kind)
        for path in images.list_versions(cfg.images_dir, kind):
            print(f"{kind:<7} {'*' if path == current else ' '} {path.name}")
    return 0


def _images_activate(cfg: config.Config, kind: str, name: str) -> int:
    images.activate(cfg.images_dir, kind, name)
    print(f"{images.LINKS[kind]} -> {name} (new VMs use it; running VMs keep theirs)")
    return 0


def _sandbox_targets() -> int:
    """This host's own addresses, as targets a job VM must not be able to reach."""
    addresses = VirshHypervisor().host_addresses(exclude=(config.BRIDGE,))
    print(" ".join(f"{address}:22" for address in addresses))
    return 0


def _cleanup(cfg: config.Config) -> int:
    github = GitHubClient(
        cfg.github.org, cfg.github.app_id, cfg.github.installation_id, cfg.github.key_file.read_text()
    )
    controller.startup_cleanup(cfg, github, VirshHypervisor())
    return 0


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=DEFAULT_CONFIG, type=Path)

    parser = argparse.ArgumentParser(prog="vivado-runners", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", parents=[common], help="run the controller (what the systemd unit starts)")
    sub.add_parser("cleanup", parents=[common], help="destroy this host's VMs and deregister its runners")
    sub.add_parser("status", parents=[common], help="show each slot")
    sub.add_parser(
        "sandbox-targets",
        help="print this host's addresses as host:port pairs for the sandbox check's extra_targets input",
    )
    img = sub.add_parser("images", help="list images or select the current one").add_subparsers(
        dest="images_command", required=True
    )
    img.add_parser("list", parents=[common])
    activate = img.add_parser(
        "activate", parents=[common], help="point the current symlink at a version (also rollback)"
    )
    activate.add_argument("kind", choices=["base", "vivado"])
    activate.add_argument("name")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "sandbox-targets":
        return _sandbox_targets()
    try:
        cfg = config.load(args.config)
        if args.command == "run":
            return controller.run(cfg, install_signals=True)
        if args.command == "cleanup":
            return _cleanup(cfg)
        if args.command == "status":
            return _status(cfg)
        if args.images_command == "list":
            return _images_list(cfg)
        return _images_activate(cfg, args.kind, args.name)
    except (config.ConfigError, images.ImageError) as error:
        print(f"vivado-runners: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
