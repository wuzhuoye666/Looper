from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def observation(
    metric: str, value: float | bool, unit: str, workload: str, **extra: Any
) -> dict[str, Any]:
    row = {
        "schemaVersion": "v1alpha1",
        "metric": metric,
        "value": value,
        "unit": unit,
        "phase": "measurement",
        "workload": workload,
        "statistic": extra.pop("statistic", "sample"),
        "attributes": extra.pop("attributes", {}),
    }
    row.update(extra)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize deterministic compression output")
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    native_path = args.output / "compression-native.json"
    try:
        native = json.loads(native_path.read_text(encoding="utf-8"))
        samples = native["samples"]
        valid = (
            native.get("schemaVersion") == "looper.compression-native/v1"
            and native.get("workload") == envelope["workload"]["id"]
            and native.get("roundtripOk") is True
            and isinstance(samples, list)
            and len(samples) >= 3
            and all(
                isinstance(item.get("latencyMs"), (int, float))
                and math.isfinite(float(item["latencyMs"]))
                and float(item["latencyMs"]) > 0
                and isinstance(item.get("throughputMiBs"), (int, float))
                and math.isfinite(float(item["throughputMiBs"]))
                and float(item["throughputMiBs"]) > 0
                for item in samples
            )
        )
        message = None if valid else "native compression output failed validation"
    except (OSError, ValueError, KeyError, TypeError) as error:
        native = {}
        samples = []
        valid = False
        message = f"could not parse native compression output: {error}"

    metrics_path = args.output / "metrics.jsonl"
    if valid:
        rows = [
            observation("roundtrip_ok", True, "bool", native["workload"], statistic="boolean"),
            observation(
                "compression_ratio",
                float(native["compressionRatio"]),
                "ratio",
                native["workload"],
                statistic="mean",
                sampleCount=len(samples),
            ),
            observation(
                "output_bytes",
                float(native["outputBytes"]),
                "bytes",
                native["workload"],
                statistic="count",
            ),
        ]
        for item in samples:
            rows.append(
                observation(
                    "latency_ms",
                    float(item["latencyMs"]),
                    "ms",
                    native["workload"],
                    sampleIndex=int(item["index"]),
                    sampleCount=len(samples),
                )
            )
            rows.append(
                observation(
                    "throughput_mib_s",
                    float(item["throughputMiBs"]),
                    "MiB/s",
                    native["workload"],
                    sampleIndex=int(item["index"]),
                    sampleCount=len(samples),
                )
            )
        metrics_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )
    else:
        metrics_path.unlink(missing_ok=True)

    log_path = args.output / "benchmark.log"
    log_path.write_text(
        f"workload={envelope['workload']['id']}\n"
        f"normalization={'succeeded' if valid else 'failed'}\n"
        + (
            f"error={message}\n"
            if message
            else f"sample_count={len(samples)}\noutput_sha256={native['outputSha256']}\n"
        ),
        encoding="utf-8",
    )
    result = {
        "schemaVersion": "v1alpha1",
        "status": "succeeded" if valid else "failed",
        "message": message,
        "checks": [
            {
                "id": "roundtrip",
                "passed": valid,
                "scope": "candidate",
                "kind": "correctness",
                "message": "every compressed sample reproduces the source payload"
                if valid
                else message,
                "details": {"outputSha256": native.get("outputSha256")},
            }
        ],
        "artifacts": [
            {
                "path": "compression-native.json",
                "role": "raw-result",
                "mediaType": "application/json",
                "description": "suite-owned compression samples and output digest",
            },
            {
                "path": "benchmark.log",
                "role": "log",
                "mediaType": "text/plain",
                "description": "compression normalizer summary",
            },
        ],
        "extensions": {"nativeSchema": "looper.compression-native/v1"},
    }
    (args.output / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
