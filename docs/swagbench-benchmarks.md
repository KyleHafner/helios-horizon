# Horizon SwagBench integration

Horizon exposes SwagBench as a typed, asynchronous profile operation. The web
client selects only root-configured preset IDs. Raw JVM arguments, filesystem
paths, commands, and report contents never cross the browser-to-controller RPC
boundary.

## Operator contract

The target profile must include the `benchmark` operation and the root
configuration must contain one matching `[[benchmark]]` entry:

```toml
[[benchmark]]
profile = "minecraft-sunlit-cobblemon"
driver = "/usr/local/libexec/swagbench-ab"
config = "/etc/game-control/swagbench.json"
report_root = "/srv/game-servers/minecraft-sunlit-benchmark/.horizon-reports"
timeout_seconds = 21600

[[benchmark.presets]]
id = "current"
label = "Current production"

[[benchmark.presets]]
id = "balanced-g1"
label = "Balanced G1 candidate"
```

The separate SwagBench driver configuration owns the actual JVM arguments and
isolated benchmark-root details. Both files and the driver must be root-owned,
must not be group/world writable, and the artifact root must be a real
root-controlled directory. Give the configured benchmark group execute-only
traversal on the artifact root (for example, `root:swagbench` mode `0710`) and
ensure every parent directory is traversable by that account; do not loosen a
private controller-state parent to achieve this. The driver launches Java as its configured
unprivileged account, then reclaims completed reports and logs to root. Horizon
invokes the driver without a shell and
accepts only a single bounded summary path beneath that configured root.

## Lifecycle and evidence

`POST /api/v1/profiles/{profile_id}/benchmarks` accepts a comparison as a
background job only after the shared game slot and target profile are proven
idle. While the benchmark job is active, normal profile starts fail with a
retryable `invalid_state` response. A controller restart marks the interrupted
job failed through the normal startup reconciliation path.

`GET /api/v1/profiles/{profile_id}/benchmarks` returns configured preset labels
and the last 20 safe summaries. Horizon retains the overall verdict, bounded
metric comparisons, bottleneck attribution, leak signal, loopback-load result,
and process duration. Full reports and process logs remain root-only artifacts.

The Benchmarks tab polls only while a run is active. A worse or mixed verdict
is evidence, not an execution failure; invalid/degraded/mismatched reports fail
the job instead of being averaged into a recommendation.
