from __future__ import annotations

from looper_core.fingerprint import _parse_cpuinfo, system_fingerprint
from looper_worker.fingerprint import worker_fingerprint


def test_cpuinfo_parser_captures_fairness_fields() -> None:
    parsed = _parse_cpuinfo(
        """processor : 0
model name : Example CPU
microcode : 0x123
flags : sse avx avx2 sse

processor : 1
model name : Example CPU
"""
    )
    assert parsed == {
        "model_name": "Example CPU",
        "microcode": "0x123",
        "flags": ["avx", "avx2", "sse"],
    }


def test_system_fingerprint_has_versioned_fairness_sections() -> None:
    fingerprint = system_fingerprint()
    assert fingerprint["schema_version"] == "looper.system-fingerprint/v1alpha1"
    assert fingerprint["logical_cpu_count"] == fingerprint["cpu"]["logical_count"]
    assert "microcode" in fingerprint["cpu"]
    assert "smt_active" in fingerprint["controls"]
    assert "transparent_hugepages" in fingerprint["controls"]
    assert "version" in fingerprint["cgroup"]
    assert isinstance(fingerprint["network_interfaces"], list)
    assert isinstance(fingerprint["storage"], list)
    assert "docker" in fingerprint["runtime"]


def test_worker_uses_shared_system_fingerprint_contract() -> None:
    fingerprint = worker_fingerprint()
    assert fingerprint["schema_version"] == "looper.system-fingerprint/v1alpha1"
    assert fingerprint["runtime"]["python"] == fingerprint["python"]
