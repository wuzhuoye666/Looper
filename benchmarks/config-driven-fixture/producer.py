from __future__ import annotations

import argparse
import json
from pathlib import Path

from looper_benchmark_sdk import load_envelope


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    envelope = load_envelope(args.envelope)
    scale = int(envelope["candidate"]["parameters"].get("scale", 1))
    items = int(envelope["workload"]["metadata"]["items"])
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "raw-result.json").write_text(
        json.dumps({"processedItems": items * scale, "valid": True}),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
