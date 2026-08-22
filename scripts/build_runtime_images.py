from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPOSITORY_ROOT / "runtimes" / "images.lock.yaml"
REGISTRY_NAME = "looper-runtime-registry"


def run(*argv: str, capture: bool = False, timeout: int = 3600) -> str:
    completed = subprocess.run(
        list(argv),
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        timeout=timeout,
    )
    return completed.stdout.strip() if capture else ""


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def registry_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:5000/v2/", timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def ensure_registry(lock: dict[str, Any]) -> None:
    if registry_ready():
        return
    registry_root = REPOSITORY_ROOT / ".looper" / "local-registry"
    storage = registry_root / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    config_path = registry_root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "distSpecVersion": "1.1.0",
                "storage": {"rootDirectory": "/var/lib/registry"},
                "http": {"address": "0.0.0.0", "port": "5000"},
                "log": {"level": "info"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["docker", "rm", "--force", REGISTRY_NAME],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    run(
        "docker",
        "run",
        "--detach",
        "--name",
        REGISTRY_NAME,
        "--publish",
        "127.0.0.1:5000:5000",
        "--mount",
        f"type=bind,source={config_path.resolve()},destination=/etc/zot/config.json,readonly",
        "--mount",
        f"type=bind,source={storage.resolve()},destination=/var/lib/registry",
        str(lock["registry"]["image"]),
        "serve",
        "/etc/zot/config.json",
        capture=True,
        timeout=120,
    )
    for _ in range(30):
        if registry_ready():
            return
        time.sleep(0.5)
    logs = run("docker", "logs", REGISTRY_NAME, capture=True, timeout=30)
    raise RuntimeError(f"local OCI registry did not become ready:\n{logs}")


def prepare_context(runtime_id: str, record: dict[str, Any]) -> Path:
    source = REPOSITORY_ROOT / record["source"]["archive"]
    if not source.is_file():
        raise RuntimeError(f"pinned source archive is missing: {source}")
    if source.stat().st_size != int(record["source"]["bytes"]):
        raise RuntimeError(f"pinned source archive size changed: {source}")
    if digest(source) != record["source"]["sha256"]:
        raise RuntimeError(f"pinned source archive digest changed: {source}")
    build_root = REPOSITORY_ROOT / ".looper" / "runtime-build" / runtime_id
    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True)
    definition_root = REPOSITORY_ROOT / "runtimes" / runtime_id
    for item in definition_root.iterdir():
        destination = build_root / item.name
        if item.is_dir():
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)
    shutil.copy2(source, build_root / "source.tar.gz")
    return build_root


def image_digest(reference: str) -> str:
    output = run(
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        reference,
        "--format",
        "{{.Manifest.Digest}}",
        capture=True,
        timeout=120,
    )
    candidates = [token for token in output.split() if token.startswith("sha256:")]
    if len(candidates) != 1 or len(candidates[0]) != 71:
        raise RuntimeError(f"could not resolve one image digest for {reference}: {output!r}")
    return candidates[0]


def build_image(runtime_id: str, record: dict[str, Any], output_root: Path) -> None:
    context = prepare_context(runtime_id, record)
    reference = str(record["local_tag"])
    run(
        "docker",
        "buildx",
        "build",
        "--platform",
        str(record["platform"]),
        "--provenance=mode=max",
        "--sbom=true",
        "--push",
        "--tag",
        reference,
        str(context),
        timeout=7200,
    )
    resolved_digest = image_digest(reference)
    immutable_reference = f"{reference.rsplit(':', 1)[0]}@{resolved_digest}"
    run("docker", "pull", immutable_reference, timeout=1800)
    image_id = run(
        "docker", "image", "inspect", immutable_reference, "--format", "{{.Id}}", capture=True
    )
    run(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        immutable_reference,
        f"looper-{runtime_id}",
        "--self-check",
        timeout=300,
    )
    output_root.mkdir(parents=True, exist_ok=True)
    sbom_path = output_root / f"{runtime_id}.spdx.json"
    run(
        "docker",
        "sbom",
        immutable_reference,
        "--format",
        "spdx-json",
        "--output",
        str(sbom_path),
        timeout=1800,
    )
    record.update(
        {
            "status": "built-and-verified"
            if runtime_id == "benchbase-smallbank"
            else "source-appliance-built-and-verified",
            "image": immutable_reference,
            "image_id": image_id,
            "sbom_path": sbom_path.relative_to(REPOSITORY_ROOT).as_posix(),
            "sbom_sha256": digest(sbom_path),
            "built_at": datetime.now(UTC).isoformat(),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build pinned Looper runtime images and SBOMs")
    parser.add_argument(
        "--runtime",
        action="append",
        choices=["benchbase-smallbank", "dcperf-mediawiki"],
        help="runtime to build; repeat to select more than one",
    )
    arguments = parser.parse_args()
    lock = yaml.safe_load(LOCK_PATH.read_text(encoding="utf-8"))
    selected = arguments.runtime or list(lock["images"])
    ensure_registry(lock)
    output_root = REPOSITORY_ROOT / ".looper" / "runtime-images"
    for runtime_id in selected:
        print(f"building {runtime_id}", flush=True)
        build_image(runtime_id, lock["images"][runtime_id], output_root)
        temporary = LOCK_PATH.with_suffix(".yaml.tmp")
        temporary.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")
        temporary.replace(LOCK_PATH)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"runtime build failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
