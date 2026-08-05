# Example deployment guidance

The checked-in `config/` and `ops/` files show the shape of a hardened deployment. They are not safe to install unchanged.

## Required adaptation

1. Create dedicated service accounts for the controller, web process, and each game server.
2. Replace example profile paths, ports, public endpoints, service names, and Crafty server ID.
3. Keep each mutable root separate from its backup root and immutable release directory.
4. Provision the Crafty token, proxy credential, and notification destinations as mode `0600` runtime files outside Git.
5. Bind the web service to loopback or a private interface reachable only by the trusted reverse proxy.
6. Configure SSO and inject both the fixed proxy credential and authenticated identity.
7. Restrict the Unix socket to the controller and web accounts; verify peer-credential rejection.
8. Validate systemd units with `systemd-analyze verify` before installing them.
9. Exercise backup creation, verification, restore, rollback, and one-profile-at-a-time slot behavior with disposable data.
10. Confirm stopped game ports and internal telemetry ports are unreachable from untrusted networks.

## Installer dry run

The installer accepts an alternate root so its filesystem output can be inspected without touching `/`:

```bash
root=$(mktemp -d)
token=$(mktemp)
printf 'synthetic-token\n' > "$token"
python3 ops/install.py --apply --root "$root" --token-source "$token" --skip-systemd-verify
python3 ops/install.py --check --root "$root" --token-source "$token" --skip-systemd-verify
```

Do not use `--skip-systemd-verify` for a real installation.

## Public-release note

The public repository was produced from a sanitized source snapshot. Operational runbooks, private topology, live identifiers, player/world data, deployment evidence, and prior internal Git history are deliberately excluded.
