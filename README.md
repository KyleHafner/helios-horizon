# Helios Horizon

[![CI](https://github.com/KyleHafner/helios-horizon/actions/workflows/ci.yml/badge.svg)](https://github.com/KyleHafner/helios-horizon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Helios Horizon is a security-focused control plane for a host that runs one resource-intensive game server at a time. Its retained reference topology coordinates Minecraft, Project Zomboid, and Terraria profiles through fixed runners behind one typed controller, instead of letting the web process invoke arbitrary shell commands.

![Responsive Horizon dashboard using synthetic fixture data](docs/assets/dashboard.png)

> The screenshots and checked-in configuration use synthetic data and documentation-only addresses. This repository contains no production credentials, player database, worlds, backups, or live infrastructure inventory.

## What it demonstrates

- **Fail-closed control boundary:** FastAPI talks to a privileged controller over a fixed Unix socket and typed newline-delimited RPC.
- **Single-slot orchestration:** starts, stops, restarts, and switches are serialized so mutually exclusive game servers cannot own the heavy-resource slot together.
- **Bounded operations:** profile IDs, units, paths, ports, console transports, timeouts, and update strategies come from reviewed configuration rather than request-supplied shell text.
- **Layered web security:** reverse-proxy credential verification, forwarded identity, expiring server-side sessions, CSRF tokens, strict origin checks, and credential redaction.
- **Recovery-aware workflows:** verified backups, prepare/confirm restore and world-clone flows, rollback-aware switching, and append-only audit records.
- **Operator visibility:** SSE status updates, logs, events, inactive-aware player history, wall-clock TPS/MSPT evidence, scheduled switches, and idle shutdown.
- **Performance experiments:** reviewed benchmark presets, persisted comparisons, and an idempotent SwagBench history importer keep tuning evidence attached to the controlled profile instead of scattered across ad-hoc reports.
- **Deployment hardening:** unprivileged service accounts, systemd sandboxing, fixed writable paths, `NoNewPrivileges`, protected homes, and root-owned runtime locks.
- **Reference operations surfaces:** sanitized examples for a console broker with loopback-only RCON, `save-off -> flush -> copy/verify -> save-on` online backups, bounded TPS scraping, fixed-profile LazyMC wake, MCP status/wake/TPS tools, and encrypted keep-two B2 reconciliation.

## Architecture

```text
Authenticated operator
        │
        ▼
Reverse proxy / SSO
        │ fixed proxy credential + identity headers
        ▼
FastAPI + static web UI (unprivileged operator path)
        │ typed RPC over /run/game-control/control.sock
        ▼
Slot controller (privileged, peer-credential checked)
        │
        ├── fixed systemd runner ── Minecraft
        ├── fixed systemd runner ── Terraria
        └── fixed systemd runner ── modded Terraria

LazyMC ── fixed Waker capability ────────┘
MCP adapter ── Observer/Waker tools ────┘
Controller backup worker ── encrypted B2 (keep two verified generations)
```

The web tier cannot choose a socket, unit, executable, or filesystem path. Mutation requests additionally require a valid session, CSRF token, and approved origin. See [architecture](docs/architecture.md) and [security model](docs/security-model.md).

## Operator experience

The dashboard separates current state from historical evidence. A stopped server is shown as intentionally inactive rather than as a zero-value telemetry source; player-hours and prior sessions remain available, while current occupancy and tick health are labelled as unknown or last observed. The Minecraft tick view charts only samples inside the selected wall-clock range, preserves gaps where Horizon observed no telemetry, and displays MSPT alongside TPS so tick headroom is visible.

Benchmark history can be recorded by Horizon directly or imported from reviewed SwagBench JSON artifacts. Import is validation-first and idempotent; see [SwagBench history import](docs/swagbench-history-import.md).

## Repository layout

- `src/game_control/` — controller, RPC protocol, API, auth, scheduling, backup, telemetry, and adapters
- `web/` — dependency-free JavaScript/CSS dashboard
- `config/` — sanitized example profiles and runner definitions
- `ops/` — installer, hardened systemd units, and fixed console helpers
- `tests/` — unit, integration, packaging, security-boundary, and Playwright browser tests
- `docs/` — public-safe architecture and deployment guidance

## Local verification

Requirements: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and a Chromium-compatible Playwright runtime.

```bash
uv sync --frozen --extra test --python 3.11
uv run --frozen --extra test --python 3.11 playwright install chromium
uv run --frozen --extra test --python 3.11 pytest -q
```

To run only the non-browser suite:

```bash
uv run --frozen --extra test --python 3.11 pytest -q --ignore=tests/browser
```

## Deployment warning

The files under `config/` and `ops/` are reviewed examples, not a turnkey deployment. A real installation must supply its own service users, paths, SSO/proxy boundary, capability credentials, game-server installation, firewall rules, backups, and restore testing. The web service binds to loopback by default. Never expose it directly to the internet or commit generated configuration and secrets.

See [example deployment guidance](docs/deployment-example.md) before adapting the installer.

The experimental Go wake lobby is intentionally not part of this public release lineage. It remains archived as private research after real-client protocol and gameplay regressions; the supported public reference continues to use the fixed LazyMC waker capability described in the operations examples.

The H1/H2/G11 operational contracts are described in the
[public operations examples](docs/operations-example.md). Those examples use
synthetic fixed-profile data, documentation-only addresses, and runtime-only
secret paths; they are not installed by the baseline example installer.

## Lineage

Horizon moved from design through six gated review revisions and an approval-gated, agent-orchestrated migration. This repository is the sanitized engineering artifact: it preserves the contracts and tests while excluding live topology, credentials, worlds, backup identifiers, and operational evidence.

## License

MIT — see [LICENSE](LICENSE).
