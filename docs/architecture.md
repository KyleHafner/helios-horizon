# Helios Horizon architecture

This document describes the reusable Horizon control plane and the reviewed
deployment shape represented by the repository. Checked-in configuration and
operations files are reference material, not a claim about a live host. Real
domains, private topology, credentials, service identities, and runtime
evidence belong in an external private deployment overlay.

The security and lifecycle invariant ledger is the normative companion for
ownership and failure guarantees:
[`docs/engineering/security-and-lifecycle-invariants.md`](engineering/security-and-lifecycle-invariants.md).

## Scope and product/deployment boundary

Horizon is a security-focused game-server control plane. Its current runtime
boundary is a fixed set of reviewed profiles sharing one active heavy slot;
profile values and deployment inventory are supplied by the reviewed profile
and runner configuration rather than by browser input. The product code is
under `src/game_control/` and `web/`. Checked-in `config/`, `ops/`, and the
operations documentation show a sanitized, deployment-shaped reference and
must be adapted before installation. Acceptance and offline migration tools
are source-only under `tools/acceptance/` and `tools/migrations/`.

An external private overlay is responsible for real endpoints, host paths,
service accounts, secret locations, backup destinations, live identifiers, and
deployment evidence. This public source does not claim that any particular
profile, endpoint, service, or metric is active.

## Repository structure and sources of truth

| Area | Owner and purpose |
| --- | --- |
| `src/game_control/` | Installed Python product: protocol models, web/API boundary, controller, slot/lease authority, domain services, adapters, persistence, telemetry, and typed runtime ownership. |
| `web/` | Browser UI assets served by the web tier. Browser input is translated into typed API actions; it is not a lifecycle authority. |
| `config/examples/` | Documentation-only synthetic configuration examples. They are not discovered by the installer. |
| `config/profiles/`, `config/runner/`, `config/game-control.toml`, `ops/` | Reviewed reference deployment inventory and service artifacts. The deployment manifest selects the package projection; values require local review. |
| `src/game_control/deployment_manifest.py` | Single typed declaration for static files, directories, links, namespaces, secrets metadata, databases, runtime sources, and generated entry points. It is the package inventory source of truth. |
| `ops/install.py` | Installer projection of the manifest into an alternate or target root. It does not make the target live merely because it wrote files. |
| `scripts/verify-deployed.py` and `src/game_control/deployment_verify.py` | Read-only verification. They independently inspect target state while consuming the source manifest's expected static inventory. |
| `tools/acceptance/` | Source-only performance, browser-evidence, and memory-pressure acceptance tools. External schema/version strings are retained where they are part of an evidence contract. |
| `tools/migrations/` | Source-only offline state and telemetry migration tools. They require their own offline locks, backups, and schema checks. |
| `tests/` | Behavioral and boundary regression suites. `tests/acceptance/` and `tests/migrations/` mirror the source-only tool boundary. |
| `docs/` | Product/security/operations references. The invariant ledger is the durable ownership contract; this document provides the system map. |

The deployment manifest is static package policy, not live proof. Installer
output, runtime manifests, cached status, and telemetry cannot replace live
service, listener, authentication, database, relay, or socket verification.

## Components and data flow

```text
Browser
  │ authenticated session, CSRF/origin, typed action only
  ▼
Trusted reverse proxy / SSO
  │ fixed proxy credential + authenticated identity
  ▼
Unprivileged FastAPI web tier
  │ HTTP routes, SSE status projection, capability boundary
  ▼
Unix RPC client ───── fixed socket / peer-credential boundary ─────► Unix RPC server
                                                                    │ bounded RPC
                                                                    ▼
                                                           Root Controller
                                             ┌───────────────┼────────────────┐
                                             ▼               ▼                ▼
                                      Slot/lease state   Domain services   Status/evidence
                                             │          backup/update/      telemetry/history
                                             ▼          restore/console          │
                                      Fixed adapters       │                      ▼
                                             ▼              ▼             Web/SSE projection
                                      systemd/game runners and bounded transports
                                             │
                                             ▼
                                      Game process / backup roots
```

`web_main.py` owns the FastAPI lifespan, HTTP authentication/session boundary,
SSE event hub, and the Unix RPC client. `api.py` maps routes to typed protocol
actions. `slotd_main.py` owns the Unix RPC server and supervisor lifecycle;
`controller.py` dispatches typed actions and coordinates the domain services.
The adapter interface and systemd adapter perform only reviewed, bounded
profile operations. See [`web_main.py`](../src/game_control/web_main.py),
[`api.py`](../src/game_control/api.py),
[`slotd_main.py`](../src/game_control/slotd_main.py),
[`controller.py`](../src/game_control/controller.py), and
[`adapters/base.py`](../src/game_control/adapters/base.py).

LazyMC and MCP are narrower callers. They enter through the capability service,
which validates an audience-bound token and typed status/wake/TPS action before
calling the same RPC boundary. They cannot select a profile, executable,
filesystem path, endpoint, credential, or operator mutation. See
[`capability.py`](../src/game_control/capability.py),
[`capability_evidence.py`](../src/game_control/capability_evidence.py), and
the capability routes in [`web_main.py`](../src/game_control/web_main.py).

## Privilege and authority map

| Boundary | Authority | Explicitly outside its authority |
| --- | --- | --- |
| Browser and web UI | Selects high-level typed actions and displays projections. | Executables, units, paths, commands, credentials, endpoints, or root state. |
| Web API/session layer | Proxy-credential validation, session/CSRF/origin checks, request bounds, typed translation, and redacted public responses. | Direct process control, game data, root state, backup roots, or root wake evidence. |
| Capability layer | Audience/role/scope/expiry/replay/rate/cooldown checks for fixed status, wake, and TPS actions. | Operator mutations, arbitrary profile selection, console commands, paths, or secrets. |
| Unix RPC server | Fixed socket ownership, peer credentials, bounded frames, request parsing, and dispatch to the root controller. | Browser identity by itself, shell strings, arbitrary RPC methods, or unbounded payloads. |
| Root controller and state authority | Lifecycle/maintenance admission, idempotency, jobs, generations, leases, confirmation state, and audit/event records. | Caller-selected resources or web-database authority. |
| Reservation store and operation lock | Exact cross-process operation ownership and slot admission; reservation identity includes profile, operation ID, generation, process identity, and expiry. | Rewriting ownership on behalf of a direct runner or stealing another live reservation. |
| Domain services/adapters | Bounded backup, restore, update, benchmark, console, health, and process work after admission. | Acquiring authority independently or accepting arbitrary paths/commands from callers. |
| Fixed runners and console transports | Reviewed profile-specific systemd/process and RCON/FIFO execution. | Alternate executables, units, FIFOs, RCON endpoints, or credentials from input. |
| Installer and verifier | Static package policy and independent target inspection. | Treating installation output as proof of live activation. |

The detailed invariant ownership is maintained in
[`security-and-lifecycle-invariants.md`](engineering/security-and-lifecycle-invariants.md).

## Lifecycle and maintenance state machines

### Profile lifecycle

The public `ObservedState` lifecycle vocabulary is finite and intentionally
distinguishes health uncertainty from lifecycle state:

```text
stopped ──► starting ──► running ──► stopping ──► stopped
   │           │             │          │
   ├─────────────────────────────────────────────► blocked
   └───────────┴─────────────┴──────────┴────────► failed
```

`StatusSnapshot.initializing` is a separate boolean bootstrap/readiness
indicator while the controller is producing its initial snapshot; it is not
an `ObservedState` member or a lifecycle transition. `StatusSnapshot` can
therefore report initialization independently of each profile's observed
state. `ObservedState` and `StatusSnapshot` are defined in
[`models.py`](../src/game_control/models.py) and
[`protocol.py`](../src/game_control/protocol.py).
`StatusService` derives `blocked` when a conflicting slot owner is present and
`failed` for a failed job; these are observed outcomes, not additional
transient lifecycle phases. See [`status.py`](../src/game_control/status.py).

`StatusService` combines fresh adapter/process/port/health observations with
root job and slot evidence to produce a typed status projection. `HealthState`
may be `unknown` when observation is unavailable; unavailable telemetry is not
converted to a healthy or zero value. Cached status and UI projections cannot
authorize a lifecycle transition. See [`status.py`](../src/game_control/status.py),
[`health.py`](../src/game_control/health.py),
and [`protocol.py`](../src/game_control/protocol.py).

### Operation and maintenance lifecycle

Every mutating lifecycle and maintenance action is admitted through the
controller and a durable operation record. Caller-stable request IDs are
claimed before side effects; retries replay the stored result or return a
bounded pending error rather than starting a second operation.

```text
request ──► idempotency claim ──► accepted ──► running ──► succeeded
                                      │           ├──────► failed
                                      │           ├──────► deferred
                                      │           └──────► cancelled (after drain)
                                      └──────────────► rejected before mutation
```

`Controller.reconcile_startup()` marks interrupted jobs and reconciles stale
reservation state before normal mutation admission. Cancellation and lease
loss are not reported as safely finished until workers and cleanup have
drained. See [`controller.py`](../src/game_control/controller.py),
[`state_db.py`](../src/game_control/state_db.py), and
[`protocol.py`](../src/game_control/protocol.py).

### Durable lease ownership

The shared cross-process protocol is:

```text
absent ── reserve under exclusive operation.lock ──► live
   ▲                         │                       │
   │                         ├─ bounded renew ──────┘
   │                         └─ exact release
   └──────────── stale/dead reconciliation ─────────
```

The reservation is exact: profile, operation ID, state generation, controller
PID, process start ticks, operation kind, and bounded expiry must match. The
direct slot runner accepts only a matching lifecycle reservation; the updater
uses an update-kind reservation and a publication guard. For each bounded
irreversible action, the publication guard acquires the exclusive operation
lock, rechecks exact ownership and inactive state, yields for the action and
the affected directory fsync, then exits the lock. The guard does not release
the reservation. The outer updater drains promotion, cleanup, and the renewal
worker before releasing that exact reservation, so a failed cleanup cannot
release or steal a replacement reservation.
See [`slot.py`](../src/game_control/slot.py),
[`ops/bin/game-slot-run`](../ops/bin/game-slot-run),
[`sunlit_update.py`](../src/game_control/sunlit_update.py), and
[`sunlit_promote.py`](../src/game_control/sunlit_promote.py).

### Backup, restore, and update publication

Backup and restore are maintenance operations, not independent filesystem
owners. Online application backup follows the fixed quiesce sequence
`save-off -> flush -> bounded copy/verify -> save-on`; cleanup is attempted on
failure and catalog metadata is published only after verification. Restore
stages and validates data, records recovery evidence, and retains rollback
handling. See [`backups.py`](../src/game_control/backups.py),
[`backup_reconcile.py`](../src/game_control/backup_reconcile.py), and
[`worlds.py`](../src/game_control/worlds.py).

Sunlit update work separates long, bounded acquisition/staging from short
publication fences. It discovers and validates an official release manifest,
performs conservative capacity checks, stages and verifies the candidate,
protects the existing backup/state, and calls the typed promotion API. Each
irreversible state/release/version/active-link or rollback action enters the
reservation-aware publication guard, which locks and unlocks around that one
action and fsyncs the affected directory. The outer updater retains ownership
through all publication and cleanup, then releases the exact reservation only
after renewal has drained. A failed or interrupted publication leaves durable
evidence for an owned retry; it does not silently claim success. See [`sunlit_update.py`](../src/game_control/sunlit_update.py),
[`sunlit_stage.py`](../src/game_control/sunlit_stage.py), and
[`sunlit_promote.py`](../src/game_control/sunlit_promote.py).

## Persistence ownership and truth

| Store/evidence | Owner and meaning |
| --- | --- |
| Controller state DB | Root-owned writer for jobs, events/audit, request idempotency, confirmations, generations, notification rules, benchmark records and controller history. `StateDatabase` enforces its approved path and thread-affine close. |
| Web DB | Web-owned sessions, CSRF state, and capability token/replay/rate/cooldown/audit state. It is not a source of lifecycle or wake truth. |
| Telemetry DB and samplers | Bounded observations and historical metrics. They are disposable evidence and never replace root jobs/reservation authority. |
| Reservation JSON and operation lock | Cross-process lease and mutation exclusivity. The direct runner reads the reservation but does not rewrite it. |
| Backup catalog/protection and journals | Verification/provenance and recovery evidence for profile backups, restore, and release publication; callers do not select their paths. |
| Status/SSE/cache | Derived operator projection with observation time and availability. It cannot authorize mutation or turn missing values into healthy/zero values. |

The store boundaries are implemented by [`state_db.py`](../src/game_control/state_db.py),
[`web_db.py`](../src/game_control/web_db.py),
[`telemetry_db.py`](../src/game_control/telemetry_db.py),
[`history_queries.py`](../src/game_control/history_queries.py),
[`slot.py`](../src/game_control/slot.py), and
[`web_main.py`](../src/game_control/web_main.py).

## Failure, recovery, and cancellation

- Startup reconciliation runs before mutation authority is exposed. Incomplete
  controller jobs are made explicit; stale reservations are reconciled rather
  than inherited by a restarted process.
- RPC, HTTP, logs, exports, archives, decompression and time-sensitive waits
  are bounded. Public errors use typed codes and redacted details.
- An idempotency claim is made before side effects. Ambiguous pending work is
  not silently replayed.
- Lease renewal loss stops further publication. The controller drains any
  worker that could still mutate before releasing the lease; exact ownership
  checks prevent a cleanup path from deleting a replacement reservation.
- Backup save-on cleanup is attempted even when copy/verification fails. A
  cleanup failure does not turn an unverified artifact into a verified backup.
- Restore and update publication use staged data, trusted identity checks,
  journals/metadata, atomic link or rename operations, directory fsync, and
  rollback/resume checks. The last known safe publication is preserved.
- `ServiceContainer`, controller, telemetry/alert runtimes, history queries,
  adapters, update services and the supervisor each have an explicit close
  owner. Close paths cancel, drain, retain the first ordinary error, and keep
  failed cleanup retryable.

Primary anchors are [`controller.py`](../src/game_control/controller.py),
[`slot.py`](../src/game_control/slot.py),
[`service_container.py`](../src/game_control/service_container.py),
[`slotd_main.py`](../src/game_control/slotd_main.py),
[`backups.py`](../src/game_control/backups.py),
[`sunlit_promote.py`](../src/game_control/sunlit_promote.py), and the
invariant ledger.

## Deployment and package boundary

`DeploymentManifest` is the single source of static package inventory. The
installer and verifier load it from the repository/package root, not from the
target or current working directory. The installer can project into a
disposable alternate root; the verifier independently checks files, modes,
owners, links, namespaces, retired artifacts, and runtime-manifest integrity.
Live service, listener, authentication, relay, database, and socket checks are
separate from static package policy. See [`deployment_manifest.py`](../src/game_control/deployment_manifest.py),
[`ops/install.py`](../ops/install.py), and
[`deployment_verify.py`](../src/game_control/deployment_verify.py).

The installed wheel contains the reusable `game_control` package. Wave 6
acceptance and migration tools remain source-only under `tools/acceptance/` and
`tools/migrations/`; their external evidence/schema vocabulary is not runtime
module naming. Systemd units and fixed helper entry points remain reviewed
deployment boundaries, not browser-selectable commands.

## Security and testing model

The primary security properties are typed authority, privilege separation,
fixed resource boundaries, safe filesystem handling, bounded transports, exact
lease ownership, durable idempotency, and truthful unavailable states. The
repository's regression suites exercise these properties rather than treating
the installer or a green UI as proof of live safety.

Key test areas include:

- controller, slot, runner, stop-fencing, startup-reconciliation,
  idempotency and lease races;
- RPC framing/peer rejection, API authentication/CSRF/origin, capability
  audiences and redaction;
- state/web/telemetry database ownership, migration and history-query bounds;
- backup, restore, update staging/publication/resume, adversarial filesystem
  inputs, and rollback behavior;
- telemetry, alerts, status truth, benchmark safety and resource-close
  cancellation;
- deployment manifest parity, alternate-root installer safety, independent
  static verification, wheel projection, browser UI, accessibility, and
  JavaScript syntax.

The test ownership is visible in `tests/`, `tests/acceptance/`, and
`tests/migrations/`; the invariant-to-test matrix is maintained in
[`security-and-lifecycle-invariants.md`](engineering/security-and-lifecycle-invariants.md).

## Roadmap and deferred work

The next product boundary is a future dynamic server-instance workflow. It is
design-only until separately reviewed and authorized. The intended concepts
are a closed reviewed `GameKind`, immutable validated `ServerInstanceId`,
reviewed `TemplateId`, typed user `Blueprint`, and root-owned validated
`CompiledProfile`; acquisition and publication must remain staged, bounded,
content-addressed, provenance-bearing and privileged only at the final
publication boundary.

The following are deliberately not current runtime capabilities:

- a CurseForge/Modrinth marketplace, add-server wizard, or arbitrary provider
  acquisition;
- browser-selected filesystem paths, systemd units, executables, scripts,
  JVM arguments, RCON endpoints, credentials or URLs;
- execution of pack-provided scripts as root;
- dynamic profile persistence or a wider public RPC;
- update-journal startup reconciliation beyond the currently implemented
  controller/backup/reservation reconciliation;
- extraction or publication of a private Helios deployment overlay;
- migration of deferred managed-tuning campaign data.

The future workflow should be documented in an Add Server ADR before code is
implemented. It must preserve the current authority boundary and provide a
rollback/provenance story; this architecture document does not authorize the
feature or any live deployment change.

## Glossary

- **Authority:** the component permitted to decide or mutate a class of state.
- **Projection:** a derived view for operators or callers that is not safety
  evidence and cannot create a lifecycle transition.
- **Profile:** a reviewed, typed configuration identity for a game service in
  the current fixed-profile runtime.
- **Lease:** a bounded exact reservation proving that one operation owns the
  shared slot.
- **Generation:** the root-state version used to fence stale operations.
- **Evidence:** a typed observation with provenance and availability semantics
  that a safety gate may consume.
- **Unavailable:** evidence or a store could not be safely read; it is exposed
  as unavailable/blocked or a typed failure rather than fabricated healthy or
  zero data.
- **Publication guard:** a short exclusive ownership fence around one bounded
  irreversible rename, link swap, metadata write, rollback action or required
  directory fsync.
- **Private overlay:** deployment-owned host inventory and policy kept outside
  the reusable public product source.
