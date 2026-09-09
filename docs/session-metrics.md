# Session recovery and current-run metrics

The Metrics tab uses the server process start time as its left boundary and the
present as its right boundary. CPU and resident memory history comes from the
retained telemetry store, so reloading a browser does not start a new chart.
Missing, unavailable and out-of-retention observations remain gaps. A stopped
server has no active-run chart; its last readings are not turned into zeroes.
Player charts share the same time axis and backfill the latest 500 retained
occupancy observations. Older unavailable coverage is left blank and labelled.

CPU is process CPU time, where one fully used core is 100%. The displayed
maximum is effective CPU capacity after affinity and cgroup restrictions.
Memory compares resident bytes against the lower of physical memory and the
effective cgroup memory limit, not the Java heap setting. Both the chart cards
and Live Rail use current/capacity formatting. Unverifiable limits are shown as
unknown; they are never inferred from an observed peak.
The probe walks cgroup ancestors and distinguishes disabled controllers from
unreadable limits, following the [Linux cgroup v2 hierarchy](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).

History is refreshed every 30 seconds while Metrics is visible. Other detail
tabs load only the small capacity projection. Requests are bound to the current
profile and process start; late responses cannot replace another run's data.
The existing Stats tab retains its independently selectable historical windows.

Session bootstrap has bounded retries and timeouts. Temporary transport
failures leave the last rendered view in place and are recoverable without a
manual reload. Rejected authentication retains the sign-in boundary and bounded
navigation behavior. Concurrent recovery shares one request, and uncertain
server mutations retain their original idempotency key.

The readiness connector occupies a separate row from its labels on desktop;
mobile uses a vertical track. Loading and failure states remain structurally
stable, without animated loading transitions.
