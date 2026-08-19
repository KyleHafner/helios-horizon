# Retained SwagBench history import

`horizon-benchmark-import` imports one reviewed campaign envelope into the
`benchmark_runs` table. It is an operator-run, maintenance-window operation:
the controller/UI services must be stopped or prevented from writing while the
import transaction runs. It does not restart services.

The input has exactly these keys: `schemaVersion`, `campaignId`, `profileId`,
`baselinePreset`, `candidatePreset`, `createdAt`, `finishedAt`,
`overallVerdict`, and `summary`. Only the retained
`minecraft-sunlit-cobblemon` campaign, `current`/`balanced-g1` presets, and
safe summary schema version 1 are accepted. Report paths, individual logs, JVM
arguments, seeds, player identities, and arbitrary extra fields are rejected.

The database and envelope paths must be absolute regular non-symlink files. The
tool validates the exact Horizon schema, writes `artifact_path` and
`error_code` as NULL, and uses `swagbench-import-<campaignId>` as a deterministic
ID. Re-running the identical envelope returns `already_present`; changed data
for an existing campaign ID fails closed.

After staging the reviewed envelope and stopping the two Horizon control
services, run:

```text
/usr/local/libexec/horizon-benchmark-import --input /absolute/campaign.json --database /absolute/state.db
```

The tool registers only the bounded summary; full reports and logs remain
protected and are never copied or exposed through Horizon.
