# CCL-Style Workload Card Fixture

This directory provides an original, synthetic workload-card example inspired only
by the general concept of describing a collective communication workload. No text,
data, or schema definition was copied from `cornell-sysphotonics/ccl-bench`.

## Mapping

- `workload.id` and `workload.title` provide normalized identity fields.
- Operation, data type, message size, and participant count describe the workload.
- `execution` contains warmup and measured iteration counts.
- `metrics` declares expected measurement names, units, and optimization directions.
- `requirements` captures example hardware and interconnect constraints.

The upstream license is unverified, so this adapter is reference-only. Do not ingest,
vendor, or redistribute upstream source based on this fixture. Verify both license and
schema at a pinned revision before implementing live integration.
