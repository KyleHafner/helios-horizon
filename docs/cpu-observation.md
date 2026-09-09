# CPU observation reporting

`tools/acceptance/cpu_phase_report.py` is a source-only, additive summary for
the timestamped observations emitted by the Phase 2.1 collector. It accepts
normalized process CPU values from the explicit `psutil_process_cpu_percent`
source and groups them separately by `slotd` or `web` and by `startup`,
`ready-empty`, `gameplay`, `stopped`, `transition`, or `unknown`.

Phase labels are endpoint-based observations. A changed endpoint snapshot or
an endpoint that cannot be classified maps to `transition` or `unknown`;
matching endpoint snapshots do not prove that the state was continuous between
samples. The initial warmup interval is excluded. Legacy raw arrays without
timing are unavailable, with no attempt to retrofit exact phases.

Each process/phase group, plus each process's `all` total, records sample
count, summed duration coverage, median, p95, and maximum. Median and p95 are
`null` below three valid non-warmup observations;
maximums are retained so a short phase cannot hide a peak. Warmup observations
are checked but excluded from every statistic.

A future wrapper that bootstraps with warmup-only data must represent that
condition explicitly as unavailable until a valid steady observation exists.
It must not modify frozen acceptance thresholds or rewrite older raw evidence.

The percentile uses the frozen floor index `floor((n - 1) * 0.95)` without
interpolation.

The reporter requires finite bounded values, consistent monotonic timestamps,
an allowlisted process and phase, and explicit normalization metadata. Legacy
CPU arrays or observations without timing are unavailable with a legacy/missing
timing reason; they are never converted to zero or retroactively assigned a
phase. The collector may classify observations during collection. This helper
does not deploy an observer, change thresholds, or establish gameplay
acceptance. `performance_collect` exposes the result as the additive
`cpuPhaseSummary` field.

Related Forge aggregate reporting should use `forge tps` and the unique overall
aggregate rather than the first dimension in a multidimensional result. A
staged source measurement is not a deployment claim.
