from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

PACKAGE_ROOT = Path("benchmarks/dcperf-mediawiki")


def load_prepare() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dcperf_prepare", PACKAGE_ROOT / "prepare.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, blocks: list[bytes], status: int = 206) -> None:
        self.blocks = iter(blocks)
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return next(self.blocks, b"")


def test_fetch_switches_from_a_slow_route_and_resumes(monkeypatch, tmp_path: Path) -> None:
    prepare = load_prepare()
    first = b"a" * 16
    second = b"b" * 16
    payload = first + second
    requests: list[object] = []
    responses = iter([FakeResponse([first]), FakeResponse([second])])

    def urlopen(request: object, timeout: int) -> FakeResponse:
        assert timeout == 180
        requests.append(request)
        return next(responses)

    ticks = iter([0.0, 20.0, 30.0, 31.0])
    monkeypatch.setattr(prepare.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(prepare.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(prepare, "DOWNLOAD_ROUTE_PROBE_SECONDS", 15.0)
    monkeypatch.setattr(prepare, "DOWNLOAD_ROUTE_MIN_MIB_PER_SECOND", 0.25)

    result = prepare.fetch(
        {
            "id": "asset",
            "url": "https://origin.invalid/asset",
            "mirrors": ["https://slow.invalid/asset"],
            "bytes": len(payload),
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        },
        tmp_path,
    )

    assert result.read_bytes() == payload
    assert len(requests) == 2
    assert requests[0].get_header("Range") is None
    assert requests[1].get_header("Range") == f"bytes={len(first)}-"


def test_measurement_hook_refreshes_only_fixture_timestamps(tmp_path: Path) -> None:
    prepare = load_prepare()
    hook = tmp_path / "packages" / "mediawiki" / "perf-record.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("upstream hook\n", encoding="utf-8")

    prepare.configure_mediawiki_measurement_hook(tmp_path)

    content = hook.read_text(encoding="utf-8")
    assert "UPDATE recentchanges SET rc_timestamp" in content
    assert "UTC_TIMESTAMP()" in content
    assert "DELETE" not in content
    assert "perf record -a -g" in content


def test_recentchanges_uses_non_eval_renderer_in_repo_authoritative_mode(tmp_path: Path) -> None:
    prepare = load_prepare()
    settings = tmp_path / "oss-performance" / "targets" / "mediawiki" / "LocalSettings.php"
    settings.parent.mkdir(parents=True)
    settings.write_text("<?php\n$wgSitename = 'fixture';\n", encoding="utf-8")

    prepare.configure_mediawiki_recentchanges_compatibility(tmp_path)
    prepare.configure_mediawiki_recentchanges_compatibility(tmp_path)

    content = settings.read_text(encoding="utf-8")
    assert "$wgDefaultUserOptions['usenewrc'] = 0;" in content
    assert content.count("Looper: use the non-eval RecentChanges renderer") == 1


@pytest.mark.parametrize(
    "output",
    [
        "HipHop VM 3.30.12 (rel)",
        "HHVM 3.30.0-dev",
        "HipHop VM 3.30",
    ],
)
def test_hhvm_330_version_is_accepted(output: str) -> None:
    prepare = load_prepare()
    assert prepare.is_expected_hhvm_version(output) is True


@pytest.mark.parametrize(
    "output",
    [
        "HipHop VM 13.30.1",
        "HipHop VM 3.300.1",
        "HHVM 4.0.0 with compatibility 3.30",
        "3.30",
        "",
    ],
)
def test_misleading_hhvm_versions_are_rejected(output: str) -> None:
    prepare = load_prepare()
    assert prepare.is_expected_hhvm_version(output) is False


def test_host_rejects_python_older_than_310(monkeypatch) -> None:
    prepare = load_prepare()
    monkeypatch.setattr(
        prepare.sys,
        "version_info",
        (3, 9, 18),
    )
    with pytest.raises(prepare.PrepareError, match="expected >=3.10"):
        prepare.check_host()


def test_source_lock_tracks_current_prepare_script() -> None:
    lock = json.loads((PACKAGE_ROOT / "source-lock.json").read_text(encoding="utf-8"))
    declared = next(
        item["sha256"] for item in lock["files"] if item["path"] == "prepare.py"
    )
    actual = "sha256:" + hashlib.sha256(
        (PACKAGE_ROOT / "prepare.py").read_bytes()
    ).hexdigest()
    assert declared == actual
