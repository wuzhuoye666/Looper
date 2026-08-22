# DCPerf Benchpress-Like Adapter Fixture

This directory demonstrates an adapter boundary for a Benchpress-like JSON result
associated with the DCPerf catalog entry. The fixture is original and synthetic. It
was not copied from DCPerf, Benchpress, or a published benchmark run.

## Mapping

- `benchmark` becomes the normalized benchmark name.
- `status` becomes the run status.
- `timestamp` becomes the run start timestamp.
- `parameters` and `system` become configuration and environment metadata.
- Values under `metrics` become normalized measurements with units and optimization
  directions supplied by `adapter.manifest.json`.

The example intentionally covers only the fields needed to exercise an adapter. It
must not be treated as an authoritative or complete upstream schema. Validate a
pinned upstream revision before adapting live output.
