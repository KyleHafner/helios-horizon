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

The public RCON example is loopback-only (for example
`127.0.0.1:25575`) and reads its credential from a runtime-only secret
mount. The password is never accepted from a browser request, command-line
argument, environment variable, audit record, or backup manifest. The
console/online-backup sequence uses fixed commands: save-off, flush, one
bounded snapshot/copy, and save-on in a failure-safe cleanup path.

## Filesystem and service controls

The example systemd units use narrow writable paths, protected homes/system directories, private temporary storage, and `NoNewPrivileges`. Runtime locks and controller state remain root-owned. The web database is separately owned by the web account.

## Secret handling

Crafty tokens, proxy credentials, and notification destinations are loaded from protected runtime files. The application redacts common bearer tokens, webhook URLs, cookies, passwords, configured secrets, and private-key blocks before returning log records.

The repository intentionally excludes live credential files, databases, logs, backups, worlds, inventories, and generated deployment evidence.

The LazyMC reference is a proxy/supervisor boundary, not a Java lifecycle
owner: its backend is a fixed loopback listener, and its helper can request
only the fixed capability wake/status operations. The B2 reference uses a
root-only runtime credential and fixed profile/remote/prefix policy; callers
cannot supply a remote, key, staging directory, retention count, or archive
path. See [operations examples](operations-example.md) for the sanitized
contract.

## Security limitations

- The examples do not configure a reverse proxy, SSO provider, firewall, game server, or backup destination.
- Operators must validate service users, permissions, restore behavior, and network exposure for their environment.
- A controller compromise is privileged by design; minimizing its accepted input and keeping the web boundary unprivileged are central controls, not a substitute for host hardening.
