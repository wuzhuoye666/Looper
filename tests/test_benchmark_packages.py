from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

import pytest
from looper_api.benchmark_packages import (
    BenchmarkPackageError,
    build_directory_package,
    install_benchmark_package,
    parse_benchmark_package,
)
from looper_api.benchmark_registration import (
    BenchmarkRegistrationRegister,
    create_registration,
    draft_from_manifest_bytes,
    register_benchmark,
)
from looper_api.benchmark_runtime import deployment_capabilities
from looper_api.models import BenchmarkRecord
from looper_api.serialization import benchmark_view
from looper_worker.package_cache import PackageCacheError, materialize_package

PACKAGE_ROOT = Path("benchmarks/phoronix-phpbench").resolve()
SYSBENCH_PACKAGE_ROOT = Path("benchmarks/sysbench").resolve()


def _zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_package_is_canonicalized_installed_and_materialized(tmp_path: Path) -> None:
    source, expected_digest = build_directory_package(PACKAGE_ROOT)
    parsed = parse_benchmark_package(source)
    assert parsed.package_digest == expected_digest

    manifest_path = install_benchmark_package(tmp_path / "control-plane", parsed)
    assert manifest_path.is_file()
    assert (manifest_path.parent / "prepare.py").is_file()

    worker_root = materialize_package(
        {
            "encoding": "base64+zip",
            "digest": parsed.package_digest,
            "data": base64.b64encode(parsed.archive_bytes).decode("ascii"),
        },
        tmp_path / "worker-cache",
    )
    assert (worker_root / "benchmark.yaml").is_file()
    assert (worker_root / "producer.py").is_file()


@pytest.mark.parametrize(
    ("package_root", "expected_files"),
    [
        (
            SYSBENCH_PACKAGE_ROOT,
            {
                "benchmark.yaml",
                "dependency-lock.json",
                "normalizer.py",
                "prepare.py",
                "producer.py",
            },
        ),
        (
            PACKAGE_ROOT,
            {
                "benchmark.yaml",
                "dependency-lock.json",
                "normalizer.py",
                "prepare.py",
                "producer.py",
                "README.md",
                "phpbench-081-patched2.zip",
                "fixtures/pts-result.json",
            },
        ),
    ],
)
def test_benchmark_archive_contains_complete_source_contract(
    package_root: Path, expected_files: set[str]
) -> None:
    archive, _digest = build_directory_package(package_root)
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        assert set(package.namelist()) == expected_files


def test_package_rejects_path_traversal_and_digest_tampering(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkPackageError, match="unsafe package path"):
        parse_benchmark_package(
            _zip({"benchmark.yaml": b"{}", "../outside.py": b"bad"})
        )

    source, digest = build_directory_package(PACKAGE_ROOT)
    with pytest.raises(PackageCacheError, match="digest verification"):
        materialize_package(
            {
                "encoding": "base64+zip",
                "digest": digest,
                "data": base64.b64encode(source + b"tampered").decode("ascii"),
            },
            tmp_path / "worker-cache",
        )


def test_executable_zip_registration_grants_explicit_local_package_trust(
    db_session, tmp_path: Path
) -> None:
    archive, _digest = build_directory_package(PACKAGE_ROOT)
    parsed = parse_benchmark_package(archive)
    manifest_path = install_benchmark_package(tmp_path / "looper-data", parsed)
    draft = draft_from_manifest_bytes(
        parsed.manifest_bytes,
        filename=parsed.manifest_name,
    )
    registration = create_registration(
        db_session,
        draft,
        package_digest=parsed.package_digest,
        package_path=str(manifest_path),
    )
    assert registration.constraints_json
    assert all(
        item["status"] == "pass"
        for item in registration.constraints_json
        if item["blocking"]
    )

    registration = register_benchmark(
        db_session,
        registration.id,
        BenchmarkRegistrationRegister(expectedRevision=1),
    )
    benchmark = db_session.get(
        BenchmarkRecord,
        "looper.phoronix-phpbench@10.8.6-phpbench1.1.6-looper12",
    )
    assert benchmark is not None
    assert benchmark.trusted is True
    assert benchmark.package_digest == parsed.package_digest
    view = benchmark_view(benchmark, registration)
    assert view["runnable"] is True
    assert view["deploymentRequirements"] == ["linux", "local-process", "python"]
    assert view["provisionedCapabilities"] == [
        "phoronix-test-suite",
        "php-cli",
        "unzip",
    ]
    assert deployment_capabilities(benchmark.manifest_json) <= {
        "linux",
        "local-process",
        "python",
    }
