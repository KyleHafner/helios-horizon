# Horizon security and lifecycle invariants

This document is the architectural contract for the reviewed Horizon source
shape. It describes authority, truth, persistence, lifecycle, failure, and
recovery boundaries. It is not an audit log, deployment record, or claim about
any particular host.

## Scope and current boundary

Horizon is a fixed-profile, single-slot controller. The web tier is
unprivileged and translates authenticated requests into typed actions. The
root controller is the authority for lifecycle and maintenance admission;
game adapters and domain services execute only bounded, admitted work.

The active packaged topology is the reviewed set of configured profiles. The
`ProfileId` enum also contains historical or candidate identifiers; its full
enumeration is not a claim that every identifier is installed or active.

Add Server, dynamic instances, provider or marketplace acquisition, compiled
templates, and private deployment-overlay extraction are future blueprint
work. They are not current browser capabilities, RPC inputs, or runtime
owners.

## Authority and privilege map

| Boundary | Owns or decides | Must not own or accept |
| --- | --- | --- |
| Root `Controller`, `StateDatabase`, and `ReservationStore` | Typed lifecycle and maintenance admission, jobs, idempotency, generations, leases, publication fences, and root wake evidence | Browser paths, commands, units, endpoints, credentials, or web-database authority |
| Web API/UI and web DB | Proxy/session authentication, CSRF and origin checks, capability grants, replay/rate/cooldown state, HTTP-facing audit, and typed translation | Process control, root state, worlds, backups, or root wake assertions |
| Domain services | Bounded adapter, archive, restore, update, and benchmark work after controller admission | Lease acquisition or caller-selected resources |
| Telemetry and alert runtimes | Disposable observations, explicit unavailable/inactive values, policy evaluation, and bounded notification delivery | Lifecycle truth or fabricated zero/healthy values |
| Fixed runners, console, RCON, and FIFO boundaries | Reviewed profile-specific process and console transport | Arbitrary executables, units, FIFOs, RCON endpoints, or secrets from input |
| Installer and verifier | Declared static package policy and independent target inspection | Treating installer output or a runtime manifest as live proof |

Direct slot runners may act only with a matching live root reservation. They do
not rewrite the root reservation file. Capability callers use a separate,
typed status/wake boundary and cannot reach the operator mutation surface.

## Persistence ownership and truth

The stores have separate owners and meanings:

- The root state DB contains jobs, events and audit records, idempotency and
  confirmation state, notification rules, benchmark history, player-session
  summaries, and legacy metric samples. `StateDatabase` is a root-owned
  writer, opened only at its approved path and closed on its owning thread.
- The web DB contains web sessions, CSRF state, and capability token,
  replay, rate, cooldown, and capability-audit state. The root reader does not
  open the web DB.
- The telemetry DB contains bounded disposable observations. It is not
  lifecycle authority and does not replace root job or reservation state.
- Reservation JSON and the operation lock provide cross-process ownership.
  Exact profile, operation, request, generation, process identity, and bounded
  expiry are checked before a worker may publish.
- Staged release, restore, backup, and domain journals are recovery evidence;
  they are not browser-selected paths or lifecycle authority.

File-backed history and telemetry reads use approved, read-only worker-local
SQLite connections. Writer connections remain thread-affine. A missing,
malformed, unreadable, or conflicting authoritative store is unavailable and
fails closed; it is never inferred to mean stopped, idle, or healthy.

## Lifecycle and health states

The public lifecycle states are deliberately finite:

```text
initializing -> stopped -> starting -> running -> stopping -> stopped
                         |             |          |
                         +-----------> failed <---+
```

`ObservedState` exposes `stopped`, `starting`, `running`, `stopping`,
`failed`, and `blocked`; it has no public `unknown` member. `HealthState`
does expose `unknown`, which is the correct representation for unavailable
health or observation evidence. A conceptual unavailable state must not be
serialized as a new lifecycle enum.

The current status implementation has a known limitation: an adapter
observation error can still derive `STOPPED` in some paths. Therefore the
stronger rule “unavailable observation never creates a stopped transition” is
a target invariant, not a claim that this exact tree already satisfies it.
Cached status is a UI/operator projection and cannot create a lifecycle
transition or satisfy a safety gate.

Startup reconciliation of currently implemented interrupted jobs, benchmark
rows, reservations, and backup recovery precedes mutation admission. Update
journal startup reconciliation is future: this tree has no
`UpdateService.reconcile_startup()` owner.

## Maintenance jobs and leases

Durable maintenance follows:

```text
none -> accepted -> running -> succeeded
                         \-> failed
                         \-> deferred
```

The API retains its stable `accepted`, `running`, `succeeded`, `failed`, and
`cancelled` response vocabulary. A scheduled backup may persist `deferred`
when its safety conditions are not met. Cancellation or lease loss becomes a
terminal durable outcome only after the worker and its cleanup have drained.

The lease protocol is:

```text
absent -> reserve under operation lock -> renew/assert -> release
```

The reservation records exact profile, operation, request, state generation,
controller process identity, and bounded expiry. Renewal loss stops further
publication, drains any worker that can still mutate, records the safe
outcome, and releases only after that drain. Scheduled and manual backup use
the same controller lease authority.

## Status truthfulness and benchmark admission

Fresh status and slot observations are evidence; a cold cached projection is
not. Status snapshots retain observation time, generation, owner/job, health,
player count, and telemetry values without converting unavailable values into
healthy or zero values.

Benchmark admission requires fresh typed status and slot evidence, root wake
evidence, no conflicting root operation or session, the approved quiet period,
storage and UPS checks, maintenance-window policy, and rollback/public-wake
policy. Driver output is immutable provenance and evidence, not benchmark
policy. `RootWakeSafetyEvidence` reads only root-owned active-job and
reservation evidence and fails closed when that evidence is unavailable.

## Failure and recovery contract

Public failures use bounded typed error codes and redacted details. Mutation
idempotency is claimed before side effects; ambiguous pending work is not
silently replayed. Destructive operations use actor- and operation-bound
prepare/confirm state.

Backup quiesce follows the save-off, flush, copy/verify, save-on sequence, with
save-on attempted from failure cleanup. Restore stages and validates data,
records a journal, and retains rollback handling. Release updates require a
trusted digest, bounded archive extraction, atomic pointer publication, and
fsync of files and directories. Benchmark preflight validates safety before
insertion, freezes provenance, and compares execution-time evidence.

Cancellation and shutdown drain accepted work before releasing its lease or
closing a writer. A failed stage must not erase the last known safe
publication. Unavailable evidence is visible as unavailable, blocked, or a
typed failure rather than being presented as green.

## Telemetry, alert, and close ownership

Modern telemetry has one runtime owner. `TelemetryRuntime` owns its sampler,
persistent RCON, and telemetry database references when marked owned;
`TelemetryCollector` receives values for collection and does not close them.
The runtime stops intake, drains its sampler/cycle and collector work, drains
the telemetry queue, and closes owned resources only after the drain.

`AlertRuntime` owns its bounded delivery tasks and does not close
`NotificationService`. `HistoryQueryService` owns its bounded query executor.
`Controller.aclose()` owns controller background worker handles, consumes
terminal results, cancels and drains workers, and lets lease cleanup finish.

The typed `ServiceContainer` implements the owner ledger and ordered hooks:

```text
Controller.aclose()
  -> owned TelemetryRuntime
  -> owned AlertRuntime
  -> owned legacy TPS sampler
  -> owned Crafty adapters and UpdateServices
  -> owned NotificationService
  -> owned HistoryQueryService
  -> owned StateDatabase (last)
```

Only `ResourceRef.owned(value)` grants a typed resource close authority;
borrowed refs remain usable and open. Shared typed adapters are closed once.
Each stage is attempted after an ordinary error, with the first ordinary
error retained. Caller cancellation is drained through all stages and
re-raised afterward; failed ledgers remain retryable. The synchronous state
database close stays on its owning thread.

At this exact source base, `slotd_main.serve()` and `ServiceSeams` still use
the pre-composition integration path. The single production assembly
finalizer, supervisor-task envelope, and delegation from `serve()` to the
finalized container are pending commit9 integration. This section documents
the proven typed owner modules and their target integration contract; it does
not claim that the current `serve()` already performs that delegation.

## Explicit tick-source matrix

Legacy TPS mode is an explicit root configuration choice, either `disabled`
or `enabled`; exporter presence never implicitly selects legacy mode. The
effective source is singular:

| Legacy mode | Exporter binding | Effective tick source | Legacy sampler/task |
| --- | --- | --- | --- |
| disabled | absent | none | none |
| disabled | present | exporter only | none |
| enabled | absent | none | legacy sampler only |
| enabled | present | legacy override; no modern tick source | legacy sampler only |

The explicit legacy override prevents duplicate tick cadence and storage
owners. Invalid or missing mode is rejected where required, with the
compatibility default applied only at the root configuration boundary.

## Deployment integrity boundary

The current installer and verifier are separate static-policy and target-
inspection tools, not a shared typed deployment manifest. Wave4 may define a
frozen manifest for static files, directories, owners and modes, links,
units/drop-ins/slices, retired paths, secret metadata, database roles, and
runtime-manifest metadata. Independent target `lstat`/inspection and live
probes for services, listeners, cgroups, authentication, database integrity,
relay, and sockets remain necessary.

Package-safety preflight and the allowance for unrelated systemd units remain
explicit. No installer output, manifest, or static package claim is live
proof, and deployment manifest completion is not a current Horizon invariant.

## Future blueprint boundary

Future design may introduce `ServerInstanceId`, `GameKind`, and `TemplateId`,
compiled profile artifacts, provider acquisition, lockfile and provenance
checks, and staged publication. Those names do not authorize dynamic browser
profile selection, arbitrary paths or commands, a wider public RPC, or a live
marketplace/provider implementation.

The current contract remains the fixed `ProfileId` boundary, reviewed active
profile topology, one-slot reservation, typed controller actions, and
root-owned process and persistence paths. Update-journal reconciliation,
dynamic instances, template compilation, provider acquisition, and private
overlay separation remain future work.

## Verification ledger

The claims above are anchored to semantic owners and existing tests rather
than mutable line numbers:

| Invariant | Source anchors | Verification suites |
| --- | --- | --- |
| Root-only lifecycle, maintenance, and wake authority | `src/game_control/controller.py`, `slot.py`, `root_state.py`, `capability_evidence.py` | `tests/test_controller.py`, `tests/test_slot_state.py`, `tests/test_slot_runner.py`, `tests/test_stop_fencing.py`, `tests/test_capability_evidence.py` |
| Web/session/capability separation | `src/game_control/web_main.py`, `web_db.py`, `capability.py`, `auth.py` | `tests/test_auth.py`, `tests/test_api_webtier.py`, `tests/test_capability.py`, `tests/test_capability_retention.py` |
| Persistence and thread ownership | `state_db.py`, `web_db.py`, `telemetry_db.py`, `history_queries.py`, `session_store.py`, `stats_queries.py` | `tests/test_db.py`, `tests/test_state_db_stats.py`, `tests/test_state_migration.py`, `tests/test_history_queries.py`, `tests/test_telemetry_db.py`, `tests/test_telemetry_migration.py` |
| Lifecycle and health truth | `models.py`, `health.py`, `status.py`, `protocol.py` | `tests/test_status.py`, `tests/test_health.py`, `tests/test_status_stats.py`, `tests/test_capability.py` |
| Lease, generation, drain, and recovery | `controller.py`, `slot.py`, `backups.py`, `backup_reconcile.py` | `tests/test_controller.py`, `tests/test_slot_state.py`, `tests/test_slot_runner.py`, `tests/test_schedule.py`, `tests/test_backups.py`, `tests/test_backup_reconcile.py` |
| Benchmark safety and frozen provenance | `benchmark_safety.py`, `driver_preflight.py`, `benchmarks.py` | `tests/test_benchmark_safety.py`, `tests/test_driver_preflight.py`, `tests/test_benchmarks.py` |
| Update, restore, and atomic publication | `updates.py`, `backups.py`, `controller.py` | `tests/test_updates.py`, `tests/test_restore.py`, `tests/test_sunlit_promote.py`, `tests/test_controller.py` |
| Telemetry, alerts, history, and typed close ownership | `runtime/telemetry.py`, `runtime/alerts.py`, `history_queries.py`, `service_container.py`, `notifications.py`, `tps.py` | `tests/test_runtime_telemetry.py`, `tests/test_runtime_alerts.py`, `tests/test_history_queries.py`, `tests/test_service_container.py`, `tests/test_notifications.py`, `tests/test_tps.py` |
| Current startup reconciliation and pending composition integration | `slotd_main.py`, `service_wiring.py` | `tests/test_slotd_main.py`, `tests/test_service_wiring.py`; commit9 remains required for finalized assembly delegation |
| Static/live deployment separation | `ops/install.py`, `scripts/verify-deployed.py` | `tests/test_packaging.py`, `tests/test_verify_deployed.py`; Wave4 manifest is future |

## Glossary

- **Authority:** the component allowed to decide or mutate a class of state.
- **Projection:** a derived view for operators or callers that is not safety
  evidence and cannot create a lifecycle transition.
- **Lease:** a bounded, exact reservation proving one operation owns the slot.
- **Generation:** the root-state version used to fence stale operations.
- **Evidence:** a typed observation with provenance and availability semantics
  that a safety gate may consume.
- **Unavailable:** evidence or a store could not be safely read; it is not a
  stopped, idle, healthy, or zero value.
- **Deferred:** a durable operation was intentionally not executed because a
  safety or scheduling condition was not met.
- **Terminal:** a job outcome that cannot continue; cancellation and lease
  loss become terminal after accepted worker cleanup has drained.
