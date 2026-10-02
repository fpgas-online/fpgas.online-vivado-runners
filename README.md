# fpgas.online Vivado runners

Self-hosted GitHub Actions runners that build FPGA bitstreams with AMD Vivado.
Every job runs in a fresh KVM virtual machine that is destroyed afterwards. A
VM can reach GitHub and nothing else.

- Design: [docs/superpowers/specs/2026-09-25-vivado-runners-design.md](docs/superpowers/specs/2026-09-25-vivado-runners-design.md)
- Measured numbers: [docs/measurements.md](docs/measurements.md)

## Layout

| Path | What |
|---|---|
| `src/vivado_runners/` | The controller service and its CLI |
| `host/` | libvirt network, nftables table and squid allowlist |
| `image/` | Scripts that build the VM image and the Vivado disk |
| `tools/` | Helpers for measurements |

## Development

    uv run pytest
    uv run ruff check .

Vivado and any image containing it stay on the runner hosts. Never upload them:
AMD's licence allows installing Vivado, not redistributing it.
