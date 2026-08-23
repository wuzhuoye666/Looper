from __future__ import annotations

import io
import zipfile

from looper_api.app import build_benchmark_configure_skill_archive


def test_benchmark_configuration_skill_archive_is_complete_and_deterministic() -> None:
    first = build_benchmark_configure_skill_archive()
    second = build_benchmark_configure_skill_archive()

    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == [
            "looper-benchmark-configure/SKILL.md",
            "looper-benchmark-configure/agents/openai.yaml",
            "looper-benchmark-configure/references/benchmark-interface.md",
            "looper-benchmark-configure/templates/infrastructure-multi-node.yaml",
            "looper-benchmark-configure/templates/infrastructure-single-node.yaml",
        ]
        skill = archive.read("looper-benchmark-configure/SKILL.md").decode()
        metadata = archive.read(
            "looper-benchmark-configure/agents/openai.yaml"
        ).decode()
        interface = archive.read(
            "looper-benchmark-configure/references/benchmark-interface.md"
        ).decode()

    assert "name: looper-benchmark-configure" in skill
    assert "$looper-benchmark-configure" in metadata
    assert "nodeGroups" in interface
