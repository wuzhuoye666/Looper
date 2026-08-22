from __future__ import annotations

import argparse
import hashlib
import random
import time
import zlib
from pathlib import Path

from looper_benchmark_sdk import emit_metric, load_envelope, write_result


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
    chunks: list[bytes] = []
    for offset in range(0, len(payload), chunk_size):
        chunks.append(compressor.compress(payload[offset : offset + chunk_size]))
    chunks.append(compressor.flush())
    return b"".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()

    envelope = load_envelope(arguments.envelope)
    output = Path(arguments.output)
    output.mkdir(parents=True, exist_ok=True)
    parameters = envelope["candidate"]["parameters"]
    workload = envelope["workload"]
    level = int(parameters["compression_level"])
    chunk_size = int(parameters["chunk_size"])
    size = int(workload["metadata"].get("size_kib", 512)) * 1024
    sample_count = int(workload["metadata"].get("samples", 24))
    seed = int(envelope["seed"])
    payload = build_payload(size, seed)

    compressed = compress(payload, level, chunk_size)
    roundtrip_ok = zlib.decompress(compressed) == payload
    ratio = len(compressed) / len(payload)
    emit_metric(
        output,
        "roundtrip_ok",
        roundtrip_ok,
        "bool",
        phase="validation",
        workload=workload["id"],
        statistic="boolean",
    )
    emit_metric(
        output,
        "compression_ratio",
        ratio,
        "ratio",
        workload=workload["id"],
        statistic="mean",
        sample_count=sample_count,
    )
    emit_metric(
        output,
        "output_bytes",
        float(len(compressed)),
        "bytes",
        workload=workload["id"],
        statistic="count",
    )

    for sample_index in range(sample_count):
        started = time.perf_counter_ns()
        current = compress(payload, level, chunk_size)
        elapsed_seconds = (time.perf_counter_ns() - started) / 1_000_000_000
        latency_ms = elapsed_seconds * 1000
        throughput = len(payload) / (1024 * 1024) / elapsed_seconds
        emit_metric(
            output,
            "latency_ms",
            latency_ms,
            "ms",
            workload=workload["id"],
            sample_index=sample_index,
            sample_count=sample_count,
        )
        emit_metric(
            output,
            "throughput_mib_s",
            throughput,
            "MiB/s",
            workload=workload["id"],
            sample_index=sample_index,
            sample_count=sample_count,
        )
        if current != compressed:
            roundtrip_ok = False

    digest = hashlib.sha256(compressed).hexdigest()
    (output / "benchmark.log").write_text(
        "\n".join(
            [
                f"workload={workload['id']}",
                f"payload_bytes={len(payload)}",
                f"compression_level={level}",
                f"chunk_size={chunk_size}",
                f"compressed_bytes={len(compressed)}",
                f"compressed_sha256={digest}",
                f"roundtrip_ok={str(roundtrip_ok).lower()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    write_result(
        output,
        {
            "schemaVersion": "v1alpha1",
            "status": "succeeded" if roundtrip_ok else "failed",
            "message": None if roundtrip_ok else "round-trip validation failed",
            "checks": [
                {
                    "id": "roundtrip",
                    "passed": roundtrip_ok,
                    "scope": "candidate",
                    "kind": "correctness",
                    "message": "decompressed bytes match the source payload",
                    "details": {"compressed_sha256": digest},
                }
            ],
            "artifacts": [
                {
                    "path": "benchmark.log",
                    "role": "log",
                    "mediaType": "text/plain",
                    "description": "benchmark execution summary",
                }
            ],
            "extensions": {},
        },
    )
    return 0 if roundtrip_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
