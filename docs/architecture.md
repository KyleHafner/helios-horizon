# Architecture

Helios Horizon separates an unprivileged operator interface from the process that may control game servers.

## Components

1. **Web UI** — dependency-free JavaScript and CSS served by FastAPI.
2. **Web API** — authenticates the proxy boundary, owns browser sessions, validates CSRF/origin on mutations, sanitizes responses, and converts routes into typed actions.
3. **Unix RPC client/server** — newline-delimited Pydantic messages over one fixed socket. The server checks peer credentials and rejects oversized, incomplete, timed-out, or unknown requests.
4. **Slot controller** — serializes operations, enforces one active heavy profile, persists jobs/audit records, and coordinates health, backup, restore, update, scheduling, and notification services.
5. **Adapters** — a bounded Crafty adapter for Minecraft and fixed systemd profile adapters for other servers.
6. **Profile and runner definitions** — reviewed TOML/JSON files that declare approved units, paths, ports, owners, timeouts, operations, and update strategies.

## Trust boundaries

```text
Browser
  │ untrusted request data
  ▼
Trusted reverse proxy / SSO
  │ fixed credential + authenticated identity
  ▼
Unprivileged web service
  │ typed actions only
  ▼
Unix socket (fixed path, peer credentials, bounded frames)
  │
  ▼
Privileged controller
  │ fixed profiles and adapters
  ▼
Game services and backup roots
```

The browser and reverse-proxy headers are not trusted by themselves. The web service validates a separately provisioned proxy credential before accepting the forwarded identity. The controller does not accept shell strings, unit names, executable paths, sockets, or backup destinations from the HTTP request.

## Data stores

- Controller state: jobs, reservations, metrics, player-session summaries, notification rules, and append-only audit records.
- Web state: expiring browser sessions and CSRF tokens.
- Backups: profile-scoped archives outside mutable game roots, with verification metadata.

These stores are intentionally separate so the unprivileged web account does not need write access to controller state or backup roots.

## Failure model

- One operation lock prevents concurrent mutations.
- One slot reservation prevents two heavy profiles from running together.
- Start/switch flows verify memory, disk, listeners, process identity, and health markers.
- Destructive actions use prepare/confirm tokens tied to the actor and operation.
- Queue sizes, request frames, logs, exports, and timeouts are bounded.
- Exceptions are converted to fixed public errors while internal paths and credentials are redacted.
