from __future__ import annotations

import argparse
import json
from pathlib import Path

from looper_benchmark_sdk import emit_metric, write_result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    native = json.loads((output / "raw-result.json").read_text(encoding="utf-8"))
    valid = native.get("valid") is True
    emit_metric(
        output,
        "fixture_score",
        float(native["processedItems"]),
        "items",
        statistic="count",
    )
    write_result(
        output,
        {
            "schemaVersion": "v1alpha1",
            "status": "succeeded" if valid else "failed",
            "checks": [
                {
                    "id": "native-result-valid",
                    "passed": valid,
                    "scope": "attempt",
                    "kind": "correctness",
                }
            ],
        },
    )
    (output / "adapter.log").write_text(
        "adapter_protocol=looper-adapter/v1\nnormalization=completed\n",
        encoding="utf-8",
    )
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
