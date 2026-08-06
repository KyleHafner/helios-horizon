# Public operations reference

This document is a sanitized deployment reference for the current H1, H2, and
G11 contracts. It is intentionally separate from the baseline installer:
feature modules and host-specific service wiring must be reviewed together
before installation.

All addresses below are documentation examples. Replace example.com and RFC
5737 addresses with values reviewed for the target environment.

## Fixed local topology

The intended ownership model is:

    trusted reverse proxy
            |
            +-- web API on 127.0.0.1:8444
            |
            +-- public LazyMC listener on 192.0.2.10:25565
                             |
                             +-- Horizon-owned backend 127.0.0.1:25566
                             +-- Horizon-owned RCON 127.0.0.1:25575
                             +-- Horizon-owned metrics 127.0.0.1:19565

The proxy is the only public web entry point. The game backend, RCON, and
metrics listener remain loopback-only. A public gameplay name such as
mc.example.com:25565 is a documentation placeholder, not a claim about a
live endpoint.

The intentional example profile is minecraft-sunlit-cobblemon. Its mutable
data, immutable release data, backup root, systemd unit, runner, ports, and
owner are all fixed by reviewed configuration. A request cannot select an
executable, path, unit, endpoint, or profile outside that configuration.

## H1: console, RCON, and online backup

The browser command route accepts only a bounded printable command for an
authenticated operator. The controller maps it to the fixed Sunlit RCON
transport; callers cannot provide the RCON host, port, password path, FIFO, or
executable.

The reference RCON contract is:

- host 127.0.0.1;
- port 25575;
- a root-owned, mode-0600 runtime credential, provisioned outside Git;
- bounded command, packet, credential, response, and timeout values;
- authentication that accepts the protocol's optional empty response-value
  packet before the authentication response; and
- generic failure messages with command and credential material redacted.

Online application backup is a controller-owned sequence:

1. prove the profile is eligible and acquire the operation/slot lease;
2. send fixed save-off and save-all flush commands;
3. perform one bounded immutable staging/copy pass;
4. verify staged bytes and manifest metadata;
5. attempt the fixed save-on cleanup command even when the copy path fails;
6. persist catalog metadata only after the quiesce and verification gates pass.

A save-on failure is fail-closed. The result is not advertised as a verified
backup merely because an archive was written.

The optional metrics exporter is fixed to http://127.0.0.1:19565/metrics.
Responses are streamed under a hard byte cap before accumulation or database
persistence. TPS/MSPT are telemetry, not a control or authentication path.

## H2: LazyMC capability wake

LazyMC is only a proxy/supervisor. It never launches, signals, stops, or
restarts Java. Its fixed helper submits token-authenticated typed wake and
status requests to Horizon, then waits for a healthy status projection.

Capability rules:

- the root-only issuer creates fixed Waker/Observer bindings;
- the Waker is bound to minecraft-sunlit-cobblemon and only status,wake;
- the Observer has status,tps and no profile binding;
- request bodies contain a request UUID and an allowlisted action only;
- profile, command, URL, credential, and path selection are not caller inputs;
- request/response sizes, token TTL, replay, rate, cooldown, and transport
  timeouts are bounded; and
- invalid or expired credentials return safe errors without token material.

After a slot_conflict, wake may report already_active only after a fresh typed
status proves that the fixed target owns the slot and is starting or running.
An unrelated owner, stopping/failed state, stale/untyped response, or failed
verification never becomes a success.

The backend listener must remain 127.0.0.1:25566; the combined server
metadata and backend configuration must be applied together before any restart.

## G11: fixed B2 reconciliation

The reconciliation entry point has no profile, remote, prefix, archive,
staging, retention, or credential arguments. It uses fixed policy from reviewed
configuration and a root-only runtime credential such as
/etc/game-control/secrets.d/example-b2-rclone.conf.

Planning and apply are separate:

1. read the local catalog/protection state and fixed remote listing;
2. validate exact synthetic profile, generation, manifest, size, and full
   SHA-256 identity;
3. refuse the entire plan on any mismatch;
4. upload only fixed current local candidates;
5. cryptcheck/verify each replacement before any prune or catalog mutation;
6. prune only exact allowlisted orphans/older candidates, never unrelated
   remote objects; and
7. update protected state only after the corresponding full verification.

Repeated plan/apply with unchanged evidence is a no-op. A protected flag may
move from 0 to 1 only after canonical protection and matching local/remote
verification. No live remote key, generation ID, archive size, hash, listing,
or backup evidence belongs in this repository.

## Example files and installation residual

- config/examples/ contains synthetic profile, runner, root-config, and
  LazyMC metadata examples.
- ops/bin/ contains the three copy-ready H1 Sunlit helper examples added in
  this lane. H2/G11 helper and unit files remain an installer-integration
  residual until the parallel source and packaging contracts are complete.
- The baseline ops/install.py intentionally does not install these files
  while the public source and packaging lanes are being composed. Do not add
  them to the installer until the corresponding source modules, profile
  schema, service accounts, and packaging tests land together.
- Before a real installation, run systemd-analyze verify against copied units,
  use a disposable root for installer checks, provision secrets through the
  host secret mechanism, and perform independent RCON, health, restore, and B2
  verification.

## Exclusions

This public reference omits host Gate11 observers, monitoring fragments,
migration plans/evidence, private topology, production domains, credentials,
live IDs, remote keys, hashes, sizes, timestamps, player/world data, and
deployment history.
