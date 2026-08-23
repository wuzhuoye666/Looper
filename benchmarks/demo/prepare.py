from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zlib
from pathlib import Path

MINIMUM_PYTHON = (3, 11)
SUITE_ID = "looper.demo.compression"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare the deterministic compression Benchmark")
    parser.add_argument("--benchmark-root", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    args = parser.parse_args()

    if sys.version_info < MINIMUM_PYTHON:
        raise SystemExit("Python 3.11 or newer is required by this Benchmark package")
    if not zlib.ZLIB_VERSION:
        raise SystemExit("the Python runtime does not provide zlib")

    lock_path = args.benchmark_root / "dependency-lock.json"
    if not lock_path.is_file():
        raise SystemExit(f"dependency lock is missing: {lock_path}")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("schemaVersion") != "looper.dependency-lock/v1":
        raise SystemExit("dependency lock has an unsupported schemaVersion")

    args.cache.mkdir(parents=True, exist_ok=True)
    marker = {
        "schemaVersion": "looper.provisioning-marker/v1",
        "benchmark": SUITE_ID,
        "dependencyLockDigest": sha256(lock_path),
        "python": ".".join(str(item) for item in sys.version_info[:3]),
        "zlibRuntime": zlib.ZLIB_RUNTIME_VERSION,
        "provides": ["python-zlib"],
    }
    destination = args.cache / "prepared.json"
    serialized = json.dumps(marker, indent=2, sort_keys=True) + "\n"
    if not destination.is_file() or destination.read_text(encoding="utf-8") != serialized:
        destination.write_text(serialized, encoding="utf-8")
    print(json.dumps(marker, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
