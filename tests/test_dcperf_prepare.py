from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

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
