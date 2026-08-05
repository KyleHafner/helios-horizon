# Security model

## Web authentication

The expected deployment has a trusted reverse proxy that performs SSO and injects an authenticated identity. Horizon requires a second fixed proxy credential before it accepts those headers. A client that reaches the application directly cannot authenticate by spoofing the identity header alone.

After proxy authentication, Horizon creates an expiring server-side session. Mutations require:

- the proxy credential and forwarded identity;
- the session cookie;
- the current CSRF token; and
- an `Origin` value from the configured allowlist.

## Privilege separation

The FastAPI process runs as an unprivileged account and cannot start services or read game data directly. It sends typed actions to a privileged controller through `/run/game-control/control.sock`. The controller checks Unix peer credentials and maps profile IDs to fixed configuration.

Console commands are bounded printable strings sent only through a profile-specific transport. The transport path and service account are fixed by reviewed code/configuration; the request cannot supply a FIFO or executable path.

## Filesystem and service controls

The example systemd units use narrow writable paths, protected homes/system directories, private temporary storage, and `NoNewPrivileges`. Runtime locks and controller state remain root-owned. The web database is separately owned by the web account.

## Secret handling

Crafty tokens, proxy credentials, and notification destinations are loaded from protected runtime files. The application redacts common bearer tokens, webhook URLs, cookies, passwords, configured secrets, and private-key blocks before returning log records.

The repository intentionally excludes live credential files, databases, logs, backups, worlds, inventories, and generated deployment evidence.

## Security limitations

- The examples do not configure a reverse proxy, SSO provider, firewall, game server, or backup destination.
- Operators must validate service users, permissions, restore behavior, and network exposure for their environment.
- A controller compromise is privileged by design; minimizing its accepted input and keeping the web boundary unprivileged are central controls, not a substitute for host hardening.
