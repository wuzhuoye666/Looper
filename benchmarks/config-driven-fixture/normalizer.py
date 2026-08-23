from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize the config-driven fixture result")
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    native_path = args.output / "raw-result.json"
    try:
        native = json.loads(native_path.read_text(encoding="utf-8"))
        valid = (
            native.get("schemaVersion") == "looper.fixture-native/v1"
            and native.get("valid") is True
            and isinstance(native.get("processedItems"), int)
            and native["processedItems"] > 0
            and native.get("workload") == envelope["workload"]["id"]
        )
        message = None if valid else "native fixture result failed contract validation"
    except (OSError, ValueError, KeyError, TypeError) as error:
        native = {}
        valid = False
        message = f"could not parse native fixture result: {error}"

    observation = {
        "schemaVersion": "v1alpha1",
        "metric": "fixture_score",
        "value": float(native.get("processedItems", 0)),
        "unit": "items",
        "phase": "measurement",
        "workload": envelope["workload"]["id"],
        "statistic": "count",
        "attributes": {"scale": native.get("scale")},
    }
    metrics_path = args.output / "metrics.jsonl"
    if valid:
        metrics_path.write_text(json.dumps(observation, sort_keys=True) + "\n", encoding="utf-8")
    else:
        metrics_path.unlink(missing_ok=True)

    log_path = args.output / "adapter.log"
    log_path.write_text(
        "adapter_protocol=looper-adapter/v1\n"
        f"normalization={'succeeded' if valid else 'failed'}\n"
        + (f"error={message}\n" if message else ""),
        encoding="utf-8",
    )
    result = {
        "schemaVersion": "v1alpha1",
        "status": "succeeded" if valid else "failed",
        "message": message,
        "checks": [
            {
                "id": "native-result-valid",
                "passed": valid,
                "scope": "attempt",
                "kind": "correctness",
                "message": "native fixture output matches its package contract"
                if valid
                else message,
                "details": {"workload": envelope["workload"]["id"]},
            }
        ],
        "artifacts": [
            {
                "path": "raw-result.json",
                "role": "raw-result",
                "mediaType": "application/json",
                "description": "suite-owned native fixture result",
            },
            {
                "path": "adapter.log",
                "role": "log",
                "mediaType": "text/plain",
                "description": "normalizer execution log",
            },
        ],
        "extensions": {"adapterProtocol": "looper-adapter/v1"},
    }
    write_json(args.output / "result.json", result)
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
