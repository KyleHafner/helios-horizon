# Sanitized deployment reference

The checked-in `config/` and `ops/` trees describe a reviewed deployment shape
for Horizon. They are public reference material, not a snapshot of any current
host, and they are not safe to install unchanged.

## Product, reference, and private policy

Product code owns typed actions, validation, lease semantics, publication
fencing, and the deployment-manifest schema. The checked-in reference owns
synthetic profile, runner, unit, path, and port choices used to exercise those
contracts. A target environment must supply an external private deployment
overlay for real domains, topology, service identities, credentials, backup
policy, and operational evidence.

Do not copy private values back into the public reference. Do not treat a
successful alternate-root install as authorization to modify a real host.

## Required target review

Before composing a private deployment:

1. Choose dedicated controller, web, and game-service identities and verify
   their ownership boundaries.
2. Compile each reviewed profile to fixed paths, ports, units, executable
   arguments, and capability audiences. None may be caller-selectable.
3. Keep mutable data, immutable releases, staging, and backup roots distinct.
4. Provision provider, proxy, RCON, backup, and notification credentials as
   protected runtime files outside Git.
5. Bind the web tier only where a trusted reverse proxy can enforce SSO and a
   second application credential.
6. Restrict the controller socket to the reviewed web identity and verify Unix
   peer-credential rejection.
7. Validate units with `systemd-analyze verify` and independently review their
   filesystem and capability restrictions.
8. Exercise backup, restore, rollback, update retry, cancellation, and
   one-profile-at-a-time behavior with disposable data.
9. Confirm backend, RCON, and telemetry listeners are unreachable from
   untrusted networks.
10. Run both installer drift checks and the independent verifier before any
    service activation.

Provider credentials and provider-specific instance identifiers are local
deployment inputs. They are not universal Horizon settings and must never be
accepted from a browser action.

## Alternate-root inspection

The installer accepts an explicit alternate root. This creates only a staged
filesystem tree and skips host systemd verification:

```bash
root=$(mktemp -d)
python3 ops/install.py --apply --root "$root" --skip-systemd-verify
install -m 0600 /dev/null \
  "$root/etc/game-control/secrets.d/horizon-b2-rclone.conf"
python3 "$root/opt/game-control/ops/install.py" \
  --check --root "$root" --skip-systemd-verify
python3 scripts/verify-deployed.py --static --root "$root"
```

The empty credential is a disposable structural fixture only. Never use
`--skip-systemd-verify` for a real installation, and never treat a static PASS
as proof of network, credential, backup, restore, or service behavior.

## Reference inventory

- `config/examples/` uses explicit `horizon-example` and `example-*`
  namespaces and documentation-only networks.
- `config/profiles/`, `config/runner/`, and `config/game-control.toml` are the
  reviewed package fixtures consumed by deployment tests and the manifest.
- `ops/` contains bounded fixed entry points, unit examples, and the package
  installer. It is deployment-shaped product material, not live inventory.
- `tools/acceptance/`, `tools/migrations/`, and `tools/quality/` are source-only
  operational or development tools and are excluded from the runtime wheel.

The public repository excludes private topology, live identifiers, worlds,
player data, databases, backup evidence, credentials, remote keys, and
deployment history.
