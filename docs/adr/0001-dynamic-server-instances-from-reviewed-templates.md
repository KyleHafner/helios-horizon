# ADR 0001: Dynamic server instances from reviewed templates

- Status: Proposed
- Date: 2026-08-30
- Decision owners: Horizon maintainers

## Context

Horizon currently maps a closed set of reviewed profile IDs to root-owned
configuration. That boundary is intentionally narrow: a browser can request a
typed lifecycle action but cannot choose an executable, unit, path, endpoint,
credential, or command line.

A future Add Server workflow needs dynamic instance identity without turning
user or provider data into privileged execution authority. Marketplace
metadata, uploaded packs, pasted provider links, and imported servers are all
untrusted acquisition inputs. They cannot become profiles merely by being
well-formed.

This ADR defines the target authority and publication model. Implementing the
marketplace, provider integrations, or Add Server UI is explicitly out of
scope for Wave 7.

## Decision

Dynamic instances will be compiled from typed user choices and versioned,
root-reviewed templates. Acquisition and validation remain unprivileged.
Privilege is used only to validate the compiled result against fixed policy and
atomically publish it under a durable operation lease.

### Identity model

The model separates five concepts:

- `GameKind` is a closed enum reviewed in product source. It selects a family
  of lifecycle, health, backup, and loader policies; it is not a free-form game
  name.
- `ServerInstanceId` is an immutable, controller-generated canonical UUID.
  Parsing rejects non-canonical encodings, nil IDs, caller-chosen filesystem
  fragments, and reuse of retired IDs. It is never derived from a friendly
  name.
- `TemplateId` identifies a versioned template in the root-reviewed template
  registry. A compiled instance binds both its ID and template digest.
- `Blueprint` is the typed, bounded user intent accepted by the unprivileged
  API.
- `CompiledProfile` is the complete root-owned execution configuration emitted
  by a reviewed compiler and accepted by the privileged controller only after
  independent validation.

A friendly name is mutable display metadata. It does not participate in paths,
units, account names, sockets, cache keys, or authorization decisions.

### Browser authority

A `Blueprint` may contain only:

- a registered provider;
- stable provider project and version IDs;
- a bounded friendly name;
- a memory choice inside template-defined limits;
- a typed backup policy;
- a typed idle-stop policy; and
- optional features drawn from the selected template's closed feature set.

The browser cannot supply filesystem paths, systemd units, executables,
working directories, arbitrary JVM arguments, environment keys, shell scripts,
RCON endpoints, credentials, arbitrary URLs, service accounts, ports, socket
names, or backup destinations.

A pasted provider link is accepted only as syntax for a registered provider
adapter. The adapter must reduce an allowlisted provider URL to stable project
and version IDs before resolution. Horizon never fetches an arbitrary
browser-supplied URL.

### Reviewed templates and compiled profiles

A versioned template owns:

- the allowed `GameKind`, provider kinds, loaders, and features;
- resource bounds and port-allocation policy;
- fixed executable and argument generation rules;
- path derivation under fixed instance roots;
- service-account and unit-generation policy;
- health, console, backup, restore, update, and idle-stop behavior;
- permitted environment variables and secret references; and
- the compiler and validator schema versions.

The compiler accepts a validated `Blueprint`, a controller-generated
`ServerInstanceId`, and verified acquisition provenance. It emits a
`CompiledProfile` with fixed paths, unit identity, runner argv, service
identity, port assignments, capability audiences, artifact digests, and
template digest. No field is copied blindly from a pack manifest or provider
response.

Compiled profiles are canonical, signed or digest-bound records stored in a
root-owned registry. The privileged boundary accepts the compiled schema, not
the original provider document or browser request.

## Provider adapter model

The unprivileged acquisition service may support adapters for:

- Modrinth catalog and version resolution;
- CurseForge catalog and version resolution;
- pasted links for registered providers;
- uploaded Modrinth packs;
- uploaded CurseForge exports or server packs;
- uploaded generic prepared server archives; and
- existing-server import.

Every adapter returns normalized acquisition metadata and artifact candidates.
Provider responses, filenames, manifests, redirects, hashes, loader claims,
and embedded configuration remain untrusted. Provider credentials are
deployment secrets and are never included in a blueprint, compiled profile,
lockfile, audit payload, or public template.

Generic prepared archives and existing-server imports require an explicitly
selected reviewed template. Import inspects and copies from a bounded source;
it never adopts caller-supplied ownership, paths, units, commands, or
credentials.

## Installation pipeline

The workflow is a durable state machine with a caller-stable operation ID. A
retry observes or resumes the original authority decision rather than creating
a second installation.

1. **Resolve unprivileged.** A registered adapter resolves stable provider IDs
   under bounded requests, redirects, response sizes, and timeouts.
2. **Download bounded artifacts.** Downloads use provider-specific host policy,
   byte and file-count ceilings, content-type checks, and cancellation.
3. **Store in a content-addressed cache.** Immutable bytes are keyed by a
   locally computed digest. Temporary names and provider filenames confer no
   authority.
4. **Verify identity.** Locally computed hashes must match reviewed provider or
   upload identity where one exists. Missing provenance remains explicit.
5. **Extract safely.** Extraction rejects traversal, absolute names, duplicate
   normalized paths, symlinks, hardlinks, devices, FIFOs, sockets, set-ID bits,
   sparse/oversized output, excessive ratios, and replacement races.
6. **Apply reviewed loader setup.** Only a template-declared loader and version
   path may run. Pack-provided scripts are data and are not executed as root.
7. **Generate the runtime command.** The template compiler emits a fixed
   executable and bounded argv. User memory and feature choices are rendered
   through typed fields, never concatenated as arbitrary arguments.
8. **Run a staged startup test.** An unprivileged, resource-limited sandbox
   checks loader output, startup health, ports, logs, and cancellation without
   production credentials or publication authority.
9. **Publish atomically.** While holding the same durable maintenance/slot
   lease used by lifecycle work, the controller revalidates fixed roots,
   ownership, modes, artifact identity, template digest, and destination
   absence, then publishes through fenced rename/link operations with durable
   state transitions.
10. **Write lockfile and provenance.** The final record binds instance ID,
    template/version/digest, normalized provider identity, artifact digests,
    loader result, compiled-profile digest, schema versions, and publication
    generation. It contains no secret values.

Each stage records `pending`, `running`, `succeeded`, `failed`, or `cancelled`
with bounded public detail. Startup reconciliation must finish interrupted
work before new mutation authority is exposed.

## Privilege and lease model

Resolution, download, cache writes, extraction, and staged validation run
without root authority. The privileged controller performs no provider network
request and parses no raw pack archive at the publication boundary.

Compilation/publication participates in the existing durable operation
reservation. Start, switch, update, restore, backup, benchmark, removal, and
instance publication cannot race for the same mutable or active resources.
Reservation ownership binds the complete controller identity and operation ID;
retries cannot take over a live owner merely by reusing an operation ID.

The compiler may request a resource class or port count. Only the privileged
policy allocator chooses actual accounts, paths, ports, units, sockets, and
capability bindings.

## Script policy

Pack-provided scripts, installers, hooks, and executable flags never run as
root. The default is no execution at all.

If future compatibility requires pack scripts, it needs a separate owner
decision and a disposable build sandbox with no production credentials,
controlled networking, read-only inputs, bounded writable output, explicit
syscall/resource policy, captured provenance, and reviewable promotion. A
successful sandbox run still does not grant privileged publication; its output
must pass the same content and template validators.

## Rollback and recovery

Publication creates a new immutable generation and changes the active instance
binding only after all prior state is durable. The previous generation and its
lockfile remain available until the new generation passes health and the
retention policy permits removal.

On interruption, startup reconciliation uses the durable state and lockfile to
finish publication, restore the previous active generation, or mark the
operation explicitly recoverable. Metadata may never claim a generation is
current unless the active binding resolves to that same validated generation.

Removal is a separate typed operation. It first disables new lifecycle
authority, proves the instance is inactive, protects or verifies required
backups, retires the immutable ID, and deletes only manifest-owned paths after
fixed-root and identity revalidation.

## Threat model

| Threat | Required control |
| --- | --- |
| Malicious provider metadata | Typed adapter output, local digesting, template validation, no provider authority at publication |
| Archive traversal or link attacks | Bounded safe extraction, no links/special files, fixed-root identity checks before every mutation |
| Browser command/path injection | Closed blueprint schema; no executable, argv, path, unit, endpoint, credential, or URL fields |
| Compromised staged server | Unprivileged sandbox, no production secrets, bounded network/resources, immutable inputs |
| Publication race or retry takeover | Durable full-owner lease, caller-stable operation ID, guarded identity rechecks, atomic publication |
| Stale or forged installation state | Canonical lockfile/provenance, template and artifact digests, active-binding proof |
| Root execution from a pack | Pack scripts disabled; any future compatibility requires a separate constrained sandbox decision |

## Migration constraints

- Existing fixed profile IDs and their authority checks remain valid until an
  explicit schema migration is implemented and verified.
- Dynamic IDs are additive; they cannot alias, rename, or silently replace a
  fixed profile.
- The deployment manifest must define the template registry, compiled-profile
  registry, fixed roots, service identities, and stale-artifact policy before
  an installer may ship dynamic instances.
- Installer and independent verifier must consume the same static inventory
  while independently inspecting compiled instances and active bindings.
- Controller, web, and telemetry databases need explicit schema migrations and
  rollback rules before storing dynamic IDs.
- Existing-server import must stage and verify a copy. It cannot convert an
  arbitrary live directory in place.
- No private deployment overlay, credential, world, backup, or installed
  service is migrated by this ADR.

## Deferred owner decisions

Implementation requires later decisions on:

- template registry review and signing workflow;
- exact UUID version and retirement retention;
- provider credential custody and API quotas;
- cache quotas, garbage collection, and cross-instance deduplication;
- port and service-account allocation policy;
- loader support and sandbox technology;
- whether any pack-script compatibility is acceptable;
- staged network policy and malware scanning;
- backup requirements before activation, upgrade, import, and removal; and
- UI/API workflow, cancellation semantics, and operator approval points.

## Consequences

The model supports multiple instances without expanding browser authority into
root-selected execution. It adds a template registry, compiler, acquisition
service, cache, sandbox, provenance format, and durable installation state
machine that must be implemented and audited before the feature can ship.

Wave 7 delivers only this design boundary. It does not authorize or implement
a marketplace, Add Server UI, provider credentials, dynamic profile storage,
pack execution, publication, deployment, or service restart.

The implementation must preserve the
[security and lifecycle invariants](../engineering/security-and-lifecycle-invariants.md)
and update the [architecture source of truth](../architecture.md) when the
design advances beyond `Proposed`.
