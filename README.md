# Helios Horizon / game-control

[![CI](https://github.com/swagsystems/helios-horizon/actions/workflows/ci.yml/badge.svg)](https://github.com/swagsystems/helios-horizon/actions/workflows/ci.yml)

Helios Horizon (`game-control`) is a reusable, security-focused control plane
for self-hosted game servers. It gives one reviewed server profile ownership of
a single active slot while keeping browser authority, privileged lifecycle
work, and mutable game data behind explicit boundaries.

## Repository boundary

The repository deliberately separates three classes of material:

- **Reusable product:** the Python runtime in [`src/game_control/`](src/game_control/),
  typed APIs, controller authority model, web console, and security invariants.
- **Sanitized reference deployment:** the checked-in [`config/`](config/),
  [`ops/`](ops/), and operations examples. These show one reviewed deployment
  shape; they are not live inventory and are not safe to install unchanged.
- **External private deployment overlay:** real domains, topology, service
  identities, credentials, backup policy, and operational evidence. That
  material does not belong in this public repository.

An operator URL is deployment-specific. This project does not publish or
discover a live console endpoint.

## Architecture

- [`game-slotd`](src/game_control/slotd_main.py) is the privileged daemon. It
  accepts typed, newline-delimited RPC over the fixed Unix socket, enforces the
  lease/slot model in [`slot.py`](src/game_control/slot.py), and reconciles
  interrupted work through [`controller.py`](src/game_control/controller.py)
  before exposing mutation authority.
- [`game-control-web`](src/game_control/web_main.py) is the unprivileged
  FastAPI tier. It projects typed actions through
  [`api.py`](src/game_control/api.py), streams status with SSE, and serves the
  small vanilla-JavaScript UI from [`web/`](web/).
- Root-reviewed profile and runner configuration maps stable IDs to fixed
  adapters, units, executable arguments, paths, ports, and capabilities.
  Browser requests cannot supply those values.
- Controller events, jobs, leases, and audit state use the controller-owned
  database; web sessions use a separate web-owned database.
- [`scripts/verify-deployed.py`](scripts/verify-deployed.py) independently
  inspects a staged or installed package against the typed deployment
  manifest. It does not trust installer output.

See the [architecture](docs/architecture.md), [security model](docs/security-model.md),
and [security and lifecycle invariant ledger](docs/engineering/security-and-lifecycle-invariants.md)
for the complete authority, persistence, and recovery model.

## Capabilities

The typed controller and console include:

- lifecycle, switching, scheduling, controller-owned idle stop, and bounded
  console commands;
- verified backup, restore, clone, configuration, and update workflows under
  one durable operation lease;
- player history, activity summaries, and truthful TPS/MSPT telemetry that
  distinguishes unavailable or stale observations from zero;
- caller-stable idempotency keys, startup reconciliation, bounded transports,
  and read-only keyset history queries; and
- fresh-JVM SwagBench comparisons using root-reviewed presets and validated
  retained artifacts.

## Development

The supported Python versions are 3.11 and 3.12. The repository uses `uv`.

```bash
uv sync --frozen --extra test
uv run pytest -q --ignore=tests/browser
uv run pytest -q tests/browser
```

Before merging, run [`scripts/check.sh`](scripts/check.sh). It runs the complete
test suite, the deterministic public-boundary scanner, JavaScript syntax
checks, and Git whitespace checks. Use `--cov` to include the coverage report.

For parallel work, create an isolated named worktree rather than sharing a
dirty checkout:

```bash
git worktree add .worktrees/<name> -b <branch> main
```

## Deployment reference

[`docs/deployment-example.md`](docs/deployment-example.md) explains how to
review the sanitized package against a target environment. Inspect the
installer with an alternate root before considering a real installation, then
use the independent verifier and the
[`docs/operations-example.md`](docs/operations-example.md) recovery guidance.

Composing or changing a private deployment overlay, installing to `/`,
provisioning credentials, and restarting services are separate operator
decisions. None is implied by the source examples in this repository.
