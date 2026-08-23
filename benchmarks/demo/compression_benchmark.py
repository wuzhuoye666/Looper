from __future__ import annotations

import argparse
import hashlib
import json
import random
import time
import zlib
from pathlib import Path


def build_payload(size: int, seed: int) -> bytes:
    generator = random.Random(seed)
    patterns = [
        b"looper-performance-evidence|" * 32,
        bytes(range(256)) * 4,
        bytes(generator.randrange(0, 32) for _ in range(1024)),
    ]
    payload = bytearray()
    while len(payload) < size:
        payload.extend(patterns[len(payload) // 1024 % len(patterns)])
    return bytes(payload[:size])


def compress(payload: bytes, level: int, chunk_size: int) -> bytes:
    compressor = zlib.compressobj(level)
    chunks = []
    for offset in range(0, len(payload), chunk_size):
        chunks.append(compressor.compress(payload[offset : offset + chunk_size]))
    chunks.append(compressor.flush())
    return b"".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description="Produce deterministic native compression samples")
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    parameters = envelope["candidate"]["parameters"]
    workload = envelope["workload"]
    level = int(parameters["compression_level"])
    chunk_size = int(parameters["chunk_size"])
    size = int(workload["metadata"].get("size_kib", 512)) * 1024
    sample_count = int(workload["metadata"].get("samples", 24))
    seed = int(envelope["seed"])
    payload = build_payload(size, seed)

    reference = compress(payload, level, chunk_size)
    samples = []
    roundtrip_ok = zlib.decompress(reference) == payload
    for sample_index in range(sample_count):
        started = time.perf_counter_ns()
        current = compress(payload, level, chunk_size)
        elapsed_seconds = (time.perf_counter_ns() - started) / 1_000_000_000
        samples.append({
            "index": sample_index,
            "latencyMs": elapsed_seconds * 1000,
            "throughputMiBs": len(payload) / (1024 * 1024) / elapsed_seconds,
        })
        if current != reference or zlib.decompress(current) != payload:
            roundtrip_ok = False

    native = {
        "schemaVersion": "looper.compression-native/v1",
        "workload": workload["id"],
        "parameters": {"compressionLevel": level, "chunkSize": chunk_size},
        "payloadBytes": len(payload),
        "outputBytes": len(reference),
        "compressionRatio": len(reference) / len(payload),
        "outputSha256": hashlib.sha256(reference).hexdigest(),
        "roundtripOk": roundtrip_ok,
        "samples": samples,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "compression-native.json").write_text(
        json.dumps(native, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0 if roundtrip_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
