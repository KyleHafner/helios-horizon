# Security model

This document describes reusable product controls and a sanitized deployment
model. It does not describe a current host. Real topology, identities,
credentials, and operational evidence belong in an external private deployment
overlay.

## Web authentication

The sanitized deployment model has a trusted reverse proxy that performs SSO
and injects an authenticated identity. Horizon requires a second fixed proxy
credential before it accepts those headers. A client that reaches the
application directly cannot authenticate by spoofing the identity header alone.

After proxy authentication, Horizon creates an expiring server-side session. Mutations require:

- the proxy credential and forwarded identity;
- the session cookie;
- the current CSRF token; and
- an `Origin` value from the configured allowlist.

## Privilege separation

The FastAPI process runs as an unprivileged account and cannot start services or read game data directly. It sends typed actions to a privileged controller through `/run/game-control/control.sock`. The controller checks Unix peer credentials and maps profile IDs to fixed configuration.

Console commands pass through a controller-owned broker and a profile-specific transport. The transport path, service account, and input bounds are fixed by reviewed code/configuration; the request cannot supply a FIFO, executable path, RCON endpoint, or credential.

The public RCON example is loopback-only (for example
`127.0.0.1:25575`) and reads its credential from a runtime-only secret
mount. The password is never accepted from a browser request, command-line
argument, environment variable, audit record, or backup manifest. The
online-backup sequence is `save-off -> flush -> copy/verify -> save-on`, with
save-on attempted from a failure-safe cleanup path.

## Filesystem and service controls

The example systemd units use narrow writable paths, protected homes/system directories, private temporary storage, and `NoNewPrivileges`. Runtime locks and controller state remain root-owned. The web database is separately owned by the web account.

## Secret handling

Proxy credentials, capability tokens, RCON credentials, backup credentials, and notification destinations are loaded from protected runtime files. The application redacts common bearer tokens, webhook URLs, cookies, passwords, configured secrets, and private-key blocks before returning log records.

The repository intentionally excludes live credential files, databases, logs,
backups, worlds, inventories, private topology, and generated deployment
evidence.

The LazyMC reference is a proxy/supervisor boundary, not a Java lifecycle
owner: its backend is a fixed loopback listener, and its Waker credential can
request only fixed status/wake operations for the bound Minecraft profile.

Automation uses a separate MCP audience. Observer credentials permit status
and TPS; Waker credentials permit status and wake. Audience, role, scopes,
expiry, rate budget, and wake cooldown are all checked independently. Neither
audience can stop, restart, switch, restore, modify configuration, or issue a
console command.

The B2 reference uses a root-only runtime credential and fixed
profile/remote/prefix policy. Application backups are encrypted and reconciled
to two verified generations; callers cannot supply a remote, key, staging
directory, retention count, or archive path. See
[operations examples](operations-example.md) for the sanitized contract.

## Security limitations

- The examples do not configure a reverse proxy, SSO provider, firewall, game server, or backup destination.
- Operators must validate service users, permissions, restore behavior, and network exposure for their environment.
- A controller compromise is privileged by design; minimizing its accepted input and keeping the web boundary unprivileged are central controls, not a substitute for host hardening.

The durable ownership, lease, filesystem, publication, transport, and recovery
rules are maintained in the
[security and lifecycle invariant ledger](engineering/security-and-lifecycle-invariants.md).
