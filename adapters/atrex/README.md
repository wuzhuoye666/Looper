# Atrex Evaluation Result Adapter Fixture

This directory demonstrates a compact adapter for an `eval_result.json`-like payload
associated with the Atrex Bench catalog entry. The data, names, and values are
original synthetic examples and do not reproduce an upstream result.

## Mapping

- `task` becomes the normalized benchmark name.
- `evaluation.success` becomes the run status.
- `candidate.id` and `candidate.parameters` identify the evaluated configuration.
- `evaluation.score`, elapsed time, and checks become normalized measurements with
  explicit units and optimization directions.

This is a fixture contract for local adapter tests, not an authoritative Atrex Bench
schema. Confirm the format against a pinned upstream revision before ingesting real
results.
