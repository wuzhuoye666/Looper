from __future__ import annotations

import io
import json
import zipfile
from hashlib import sha256

import pytest
import yaml

from looper_core.system_opt.collector import (
    COLLECTION_BUNDLE_MANIFEST_NAME,
    CollectionArtifactBundleManifest,
    CollectionArtifactBundleMember,
    MetricAvailability,
    parse_collection_artifact_bundle_metrics,
    verify_collection_artifact_bundle,
)


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _verified(files: dict[str, tuple[str, bytes]]):
    manifest = CollectionArtifactBundleManifest(
        members=[
            CollectionArtifactBundleMember(
                path=path,
                media_type=media_type,
                size_bytes=len(content),
                digest=_digest(content),
            )
            for path, (media_type, content) in files.items()
        ]
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            COLLECTION_BUNDLE_MANIFEST_NAME,
            json.dumps(manifest.model_dump(mode="json")),
        )
        for path, (_, content) in files.items():
            archive.writestr(path, content)
    return verify_collection_artifact_bundle(output.getvalue(), expected_digest=manifest.digest)


def test_stress_ng_yaml_is_parsed_as_cpu_distribution() -> None:
    first = yaml.safe_dump(
        {"metrics": [{"stressor": "cpu", "bogo-ops-per-second-real-time": 9426.091273}]}
    ).encode()
    second = yaml.safe_dump(
        {"metrics": [{"stressor": "cpu", "bogo-ops-per-second-real-time": 9500.0}]}
    ).encode()
    bundle = _verified(
        {
            "raw/cpu-1.yaml": ("application/vnd.stress-ng.metrics+yaml", first),
            "raw/cpu-2.yaml": ("application/vnd.stress-ng.metrics+yaml", second),
        }
    )

    metrics = parse_collection_artifact_bundle_metrics(
        bundle,
        component="cpu",
        requested_metrics=["cpu.bogo-ops-per-second", "cpu.success"],
        gate_values={"cpu.success": True},
    )

    assert metrics["cpu.bogo-ops-per-second"].value == [9426.091273, 9500.0]
    assert metrics["cpu.success"].value == [1.0]


def test_sysbench_text_is_parsed_as_memory_distributions() -> None:
    output = b"1024.00 MiB transferred (512.25 MiB/sec)\n95th percentile: 3.75\n"
    bundle = _verified(
        {"raw/memory-1.txt": ("text/vnd.sysbench.memory", output)}
    )

    metrics = parse_collection_artifact_bundle_metrics(
        bundle,
        component="memory",
        requested_metrics=[
            "memory.bandwidth-mib-per-second",
            "memory.latency-p95-ms",
            "memory.success",
        ],
        gate_values={"memory.success": True},
    )

    assert metrics["memory.bandwidth-mib-per-second"].value == [512.25]
    assert metrics["memory.latency-p95-ms"].value == [3.75]


def test_iperf3_json_is_parsed_as_network_distributions() -> None:
    output = json.dumps(
        {
            "end": {
                "sum_received": {"bits_per_second": 2_500_000_000},
                "sum_sent": {"retransmits": 4},
            }
        }
    ).encode()
    bundle = _verified({"raw/net-1.json": ("application/vnd.iperf3+json", output)})

    metrics = parse_collection_artifact_bundle_metrics(
        bundle,
        component="network",
        requested_metrics=[
            "network.receive-throughput-gbps",
            "network.retransmits",
            "network.success",
        ],
        gate_values={"network.success": True},
    )

    assert metrics["network.receive-throughput-gbps"].value == [2.5]
    assert metrics["network.retransmits"].value == [4.0]


def test_fio_json_uses_storage_names_and_max_job_p99() -> None:
    output = json.dumps(
        {
            "jobs": [
                {
                    "error": 0,
                    "read": {
                        "iops": 1000.0,
                        "io_bytes": 4096,
                        "clat_ns": {"percentile": {"99.000000": 250000.0}},
                    },
                },
                {
                    "error": 0,
                    "read": {
                        "iops": 500.0,
                        "io_bytes": 4096,
                        "clat_ns": {"percentile": {"99.000000": 300000.0}},
                    },
                },
            ]
        }
    ).encode()
    bundle = _verified({"raw/fio-1.json": ("application/vnd.fio+json", output)})

    metrics = parse_collection_artifact_bundle_metrics(
        bundle,
        component="storage",
        requested_metrics=[
            "storage.read-iops",
            "storage.read-clat-p99-us",
            "storage.success",
        ],
        gate_values={"storage.success": True},
    )

    assert metrics["storage.read-iops"].value == [1500.0]
    assert metrics["storage.read-clat-p99-us"].value == [300.0]
    assert metrics["storage.success"].value == [1.0]


def test_gate_false_is_preserved_as_numeric_evidence_without_parsing_a_verdict() -> None:
    output = json.dumps(
        {"end": {"sum_received": {"bits_per_second": 1}, "sum_sent": {}}}
    ).encode()
    bundle = _verified({"raw/net.json": ("application/vnd.iperf3+json", output)})

    metrics = parse_collection_artifact_bundle_metrics(
        bundle,
        component="network",
        requested_metrics=["network.success"],
        gate_values={"network.success": False},
    )

    assert metrics["network.success"].value == [0.0]
    assert metrics["network.success"].availability == MetricAvailability.READABLE


def test_requested_metric_without_a_matching_raw_member_fails_closed() -> None:
    bundle = _verified({"raw/note.txt": ("text/plain", b"not a tool output")})

    with pytest.raises(ValueError, match="requested artifact metrics are unavailable"):
        parse_collection_artifact_bundle_metrics(
            bundle,
            component="cpu",
            requested_metrics=["cpu.bogo-ops-per-second"],
            gate_values={},
        )
