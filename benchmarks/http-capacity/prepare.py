from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    required = [args.benchmark_root / "runner.py", args.benchmark_root / "normalizer.py"]
    return 0 if all(path.is_file() for path in required) else 2


if __name__ == "__main__":
    raise SystemExit(main())
