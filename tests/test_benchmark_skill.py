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
        ]
        skill = archive.read("looper-benchmark-configure/SKILL.md").decode()
        metadata = archive.read(
            "looper-benchmark-configure/agents/openai.yaml"
        ).decode()

    assert "name: looper-benchmark-configure" in skill
    assert "$looper-benchmark-configure" in metadata
