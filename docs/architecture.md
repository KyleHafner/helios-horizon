# Architecture

Helios Horizon separates an unprivileged operator interface from the process that may control game servers.

## Components

1. **Web UI** — dependency-free JavaScript and CSS served by FastAPI.
2. **Web API** — authenticates the proxy boundary, owns browser sessions, validates CSRF/origin on mutations, sanitizes responses, and converts routes into typed actions.
3. **Unix RPC client/server** — newline-delimited Pydantic messages over one fixed socket. The server checks peer credentials and rejects oversized, incomplete, timed-out, or unknown requests.
4. **Slot controller** — serializes operations, enforces one active heavy profile, persists jobs/audit records, and coordinates health, backup, restore, update, scheduling, and notification services.
5. **Console broker** — sends approved, bounded commands through the fixed transport for each profile: loopback RCON for Minecraft and controller-created FIFOs for Terraria variants.
6. **Fixed systemd runners** — the retained topology has exactly three heavy profiles: one Minecraft profile and two Terraria variants. No external panel owns their lifecycle.
7. **Profile and runner definitions** — reviewed TOML/JSON files that declare approved units, paths, ports, owners, timeouts, operations, and update strategies.
8. **Capability API and MCP adapter** — separate audience-bound tokens expose only fixed status, wake, and TPS actions to LazyMC or automation clients; they never expose the operator mutation surface.

The optional public operations reference extends this shape with three
bounded paths: a loopback-only RCON/console transport and online-backup
quiesce sequence, a fixed-profile LazyMC capability wake path, MCP
status/wake/TPS tools, and a fixed-target encrypted B2 reconciliation worker.
These are documented in
[operations examples](operations-example.md); they do not make the web tier a
direct game-process or backup-remote owner.

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

Two narrower callers join the controller through the capability API, not the operator session:

- **LazyMC** presents a fixed Waker credential bound to the Minecraft profile. It may ask for status or wake only; Horizon remains the Java lifecycle owner.
- **MCP adapter** presents audience-bound Observer or Waker credentials. Observer permits status and TPS; Waker permits status and wake. Neither role can stop, restart, switch, restore, change configuration, or issue console commands.

The capability audience is checked independently of its role and scopes, so a token minted for LazyMC cannot be replayed by the MCP adapter or vice versa.

The browser and reverse-proxy headers are not trusted by themselves. The web service validates a separately provisioned proxy credential before accepting the forwarded identity. The controller does not accept shell strings, unit names, executable paths, sockets, or backup destinations from the HTTP request.

## Data stores

- Controller state: jobs, reservations, metrics, player-session summaries, notification rules, and append-only audit records.
- Web state: expiring browser sessions and CSRF tokens.
- Backups: profile-scoped archives outside mutable game roots, with verification metadata and an encrypted fixed-target remote retaining two verified generations per profile.

These stores are intentionally separate so the unprivileged web account does not need write access to controller state or backup roots.

## Failure model

- One operation lock prevents concurrent mutations.
- One slot reservation prevents two heavy profiles from running together.
- Start/switch flows verify memory, disk, listeners, process identity, and health markers.
- Destructive actions use prepare/confirm tokens tied to the actor and operation.
- Queue sizes, request frames, logs, exports, and timeouts are bounded.
- Exceptions are converted to fixed public errors while internal paths and credentials are redacted.
- Online backup persistence follows `save-off -> flush -> copy/verify -> save-on`.
  The copied staging artifact is immutable before remote upload, and a failed
  quiesce, verification, or bounded save-on attempt fails closed.
- Capability wake accepts only fixed typed status/wake actions. A wake token
  cannot select a profile, command, endpoint, or secret.
- B2 reconciliation plans before applying, verifies exact synthetic manifest
  identity and remote content, prunes only allowlisted objects, and treats
  repeated apply as a no-op.
