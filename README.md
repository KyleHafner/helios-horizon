# Helios Horizon / game-control

[![CI](https://github.com/swagsystems/helios-horizon/actions/workflows/ci.yml/badge.svg)](https://github.com/swagsystems/helios-horizon/actions/workflows/ci.yml)

Helios Horizon (`game-control`) is a single-slot game-server orchestrator:
exactly one reviewed game profile owns the active slot at a time. The operator
console URL is deployment-specific. The
[deployment guide](docs/deployment-example.md) and
[operations guide](docs/operations-example.md) cover deployment and
live-operation details.

## Architecture

- [`game-slotd`](src/game_control/slotd_main.py) is the privileged daemon. It
  exposes typed, newline-delimited RPC over
  `/run/game-control/control.sock`, uses the slot/lease model in
  [`slot.py`](src/game_control/slot.py), and reconciles interrupted work at
  startup through [`controller.py`](src/game_control/controller.py).
- [`game-control-web`](src/game_control/web_main.py) is the unprivileged
  FastAPI web tier. It projects the typed RPC contract through
  [`api.py`](src/game_control/api.py), publishes status with SSE, and serves
  the small vanilla-JavaScript Carbon UI from [`web/`](web/).
- The controller selects the fixed [`systemd`](src/game_control/adapters/systemd.py)
  adapter for the three retained VM profiles from the root-owned profiles in
  `/etc/game-control/profiles.d`. Crafty/PZ files remain source history only.
- Durable controller events and audit/jobs live in the root-owned state DB
  ([`state_db.py`](src/game_control/state_db.py)); web sessions use the
  separate web DB ([`web_db.py`](src/game_control/web_db.py)).
- [`scripts/verify-deployed.py`](scripts/verify-deployed.py) is the read-only
  deployment verifier. Use `--root PATH --static` for a staged package and follow
  [`docs/operations-example.md`](docs/operations-example.md) for how to run it and
  interpret its results.

## Feature highlights

The console and typed controller currently include:

- schedule management, including live schedule editing via
  [`schedule.py`](src/game_control/schedule.py) and
  [`schedule_config.py`](src/game_control/schedule_config.py);
- opt-in controller-owned idle-stop via
  [`idle_stop.py`](src/game_control/idle_stop.py);
- per-game player statistics, leaderboards, activity heatmaps, and Minecraft
  TPS/MSPT telemetry via [`stats_queries.py`](src/game_control/stats_queries.py),
  [`players.py`](src/game_control/players.py), and [`tps.py`](src/game_control/tps.py);
- a command palette in [`web/palette.js`](web/palette.js);
- typed, fail-closed profile configuration editing in
  [`profile_config.py`](src/game_control/profile_config.py);
- fresh-JVM SwagBench A/B jobs with root-owned preset selection, artifact
  validation, retained verdicts, and operator UI in
  [`benchmarks.py`](src/game_control/benchmarks.py); and
- one shared durable operation lease across lifecycle, backup, restore,
  update, clone, configuration, and benchmark work, with caller-stable
  idempotency keys, bounded transports, startup reconciliation, read-only
  keyset history queries, and identity-free database-growth telemetry.

## Development

This repository uses `uv` and Python 3.11+.

```bash
uv sync
uv run pytest -q
uv run pytest tests/browser -q
```

Before merging, run [`scripts/check.sh`](scripts/check.sh) for the full quality gate; pass `--cov` when you also want the pytest coverage report.

The browser suite uses Playwright and is implemented in
[`tests/browser/`](tests/browser/). For parallel feature work, create an
isolated checkout under `.worktrees/` on a named branch, for example:

```bash
git worktree add .worktrees/<name> -b <branch> main
```

## Deployment overview

The package installer applies the repository’s daemon, web, three retained
profiles, runners, and systemd artifacts to the Horizon VM through [`ops/install.py`](ops/install.py)
(`--apply`). After a controlled deployment, run the read-only verifier linked
above. The complete operational procedure, safety boundaries, and rollback
guidance is in [`docs/operations-example.md`](docs/operations-example.md); this
README intentionally does not duplicate that runbook.
