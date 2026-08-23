from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Produce the native config-driven fixture result")
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    workload = envelope["workload"]
    items = int(workload["metadata"]["items"])
    scale = int(envelope["candidate"]["parameters"]["scale"])
    native = {
        "schemaVersion": "looper.fixture-native/v1",
        "workload": workload["id"],
        "processedItems": items * scale,
        "scale": scale,
        "valid": True,
    }

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "raw-result.json").write_text(
        json.dumps(native, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
