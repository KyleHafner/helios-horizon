# Historical SwagBench history-import design

This page records an unsupported design explored for importing one reviewed
campaign envelope into the `benchmark_runs` table. Horizon does not ship or
install a SwagBench history importer, and there is no supported command or
operator procedure for performing this import.

The proposed envelope had exactly these keys: `schemaVersion`, `campaignId`,
`profileId`, `baselinePreset`, `candidatePreset`, `createdAt`, `finishedAt`,
`overallVerdict`, and `summary`. The design limited imports to the retained
`minecraft-sunlit-cobblemon` campaign, `current`/`balanced-g1` presets, and
safe summary schema version 1. Report paths, individual logs, JVM arguments,
seeds, player identities, and arbitrary extra fields were excluded.

The unimplemented design also required absolute regular non-symlink database
and envelope paths, exact Horizon schema validation, deterministic IDs, and
idempotent replay. It retained only a bounded summary while leaving full
reports and logs protected.

Any future importer requires a separately approved offline-tool and schema
plan, implementation, adversarial review, packaging proof, and maintenance
procedure. This historical note is not execution guidance.
