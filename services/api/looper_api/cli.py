from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import typer
from looper_core.action_loop import VerificationPolicy
from looper_core.adapters import load_and_apply_adapter
from looper_core.manifest import load_and_validate_manifest
from rich.console import Console
from sqlalchemy import select

from looper_api.cloud_adoption import adopt_cloud_target
from looper_api.cloud_setup import configure_cloud_purchase, credential_fields
from looper_api.database import init_database, session_scope
from looper_api.evidence import verify_evidence_bundle
from looper_api.models import ExperimentRecord
from looper_api.scheduler import create_demo_request, create_experiment, start_experiment
from looper_api.seed import seed_system
from looper_api.source_manager import (
    SourcePolicyError,
    fetch_source,
    load_source_lock,
    resolve_source,
)
from looper_api.verified_demo import run_verified_compression_loop

app = typer.Typer(help="Looper control-plane utilities")
benchmark_app = typer.Typer(help="Benchmark contract tools")
adapter_app = typer.Typer(help="Benchmark adapter tools")
evidence_app = typer.Typer(help="Evidence bundle tools")
source_app = typer.Typer(help="Third-party source governance")
demo_app = typer.Typer(help="Local demo experiment")
cloud_app = typer.Typer(help="Multi-cloud runtime configuration")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(adapter_app, name="adapter")
app.add_typer(evidence_app, name="evidence")
app.add_typer(source_app, name="source")
app.add_typer(demo_app, name="demo")
app.add_typer(cloud_app, name="cloud")
console = Console()
error_console = Console(stderr=True)


@app.command("init")
def initialize() -> None:
    init_database()
    with session_scope() as session:
        seed_system(session)
    console.print("Looper metadata and built-in benchmark are ready.")


@benchmark_app.command("validate")
def validate_benchmark(path: Path) -> None:
    manifest, digest = load_and_validate_manifest(path)
    console.print_json(
        json.dumps(
            {
                "valid": True,
                "id": manifest["metadata"]["id"],
                "version": manifest["metadata"]["version"],
                "digest": digest,
            }
        )
    )


@adapter_app.command("apply")
def apply_adapter_command(manifest: Path, input_path: Path) -> None:
    console.print_json(json.dumps(load_and_apply_adapter(manifest, input_path)))


@evidence_app.command("verify")
def verify_evidence(path: Path) -> None:
    console.print_json(json.dumps(verify_evidence_bundle(path)))


@cloud_app.command("adopt")
def adopt_cloud(
    provider: str = typer.Argument(help="Provider: tencent, alibaba, volcengine, or baidu"),
    instance_id: str = typer.Argument(),
    region: str = typer.Option(..., "--region"),
    zone: str = typer.Option(..., "--zone"),
    name: str = typer.Option(..., "--name"),
    instance_type: str = typer.Option(..., "--instance-type"),
    image_id: str = typer.Option(..., "--image-id"),
    state: str = typer.Option("RUNNING", "--state"),
    cpu: int | None = typer.Option(None, "--cpu", min=1),
    memory_gib: float | None = typer.Option(None, "--memory-gib", min=0.25),
    private_ip: str | None = typer.Option(None, "--private-ip"),
    public_ip_present: bool = typer.Option(False, "--public-ip/--no-public-ip"),
    vpc_id: str | None = typer.Option(None, "--vpc-id"),
    subnet_id: str | None = typer.Option(None, "--subnet-id"),
    source: str = typer.Option("external-adoption", "--source"),
) -> None:
    init_database()
    try:
        with session_scope() as session:
            record = adopt_cloud_target(
                session,
                provider=provider,
                region=region,
                zone=zone,
                instance_id=instance_id,
                name=name,
                instance_type=instance_type,
                image_id=image_id,
                state=state,
                cpu=cpu,
                memory_gib=memory_gib,
                private_ip=private_ip,
                public_ip_present=public_ip_present,
                vpc_id=vpc_id,
                subnet_id=subnet_id,
                source=source,
            )
            target_id = record.id
    except ValueError as error:
        error_console.print(f"[red]cloud adoption error:[/red] {error}")
        raise typer.Exit(code=2) from error
    console.print(target_id)


@cloud_app.command("configure")
def configure_cloud(
    provider: str = typer.Argument(help="Provider: tencent or alibaba"),
    env_file: Path = typer.Option(Path(".env"), "--env-file"),
    max_hourly_amount: float = typer.Option(10.0, "--max-hourly-amount", min=0.01),
) -> None:
    try:
        fields = credential_fields(provider)
        values: dict[str, str] = {}
        for variable, label, required in fields:
            prompt = f"{provider.title()} {label}"
            values[variable] = typer.prompt(
                prompt,
                default=None if required else "",
                hide_input=True,
                show_default=False,
            )
        result = configure_cloud_purchase(
            provider,
            values,
            env_file=env_file,
            max_hourly_amount=Decimal(str(max_hourly_amount)),
        )
    except ValueError as error:
        error_console.print(f"[red]cloud configuration error:[/red] {error}")
        raise typer.Exit(code=2) from error

    console.print(f"Cloud purchase configuration written to {result.env_file}")
    console.print(f"Provider allowlisted: {result.provider}")
    console.print(f"Hourly spend cap: {result.max_hourly_amount}")
    console.print("Restart the Looper API, then enter this Operator token in the Web key control:")
    console.print(result.operator_token)


@source_app.command("list")
def list_sources(
    lock_path: Path = Path("third_party/sources.lock.yaml"),
) -> None:
    lock = load_source_lock(lock_path)
    rows = [
        {
            "id": item["id"],
            "license": item.get("license"),
            "status": item["inclusion_status"],
            "commit": item.get("commit"),
        }
        for item in lock["sources"]
    ]
    console.print_json(json.dumps(rows))


@source_app.command("resolve")
def resolve_source_command(
    source_id: str,
    lock_path: Path = Path("third_party/sources.lock.yaml"),
) -> None:
    try:
        result = resolve_source(lock_path, source_id)
    except SourcePolicyError as error:
        error_console.print(f"[red]source policy error:[/red] {error}")
        raise typer.Exit(code=2) from error
    console.print_json(json.dumps(result))


@source_app.command("fetch")
def fetch_source_command(
    source_id: str,
    lock_path: Path = Path("third_party/sources.lock.yaml"),
    cache_root: Path = Path(".looper/upstreams"),
) -> None:
    try:
        result = fetch_source(lock_path, source_id, cache_root)
    except SourcePolicyError as error:
        error_console.print(f"[red]source policy error:[/red] {error}")
        raise typer.Exit(code=2) from error
    console.print_json(json.dumps(result))


@demo_app.command("create")
def create_demo(name: str = "Compression Pareto study", start: bool = False) -> None:
    init_database()
    with session_scope() as session:
        seed_system(session)
        experiment = create_experiment(session, create_demo_request(name))
        if start:
            start_experiment(session, experiment)
        console.print(experiment.id)


@demo_app.command("start")
def start_demo(experiment_id: str) -> None:
    with session_scope() as session:
        experiment = session.scalar(
            select(ExperimentRecord).where(ExperimentRecord.id == experiment_id)
        )
        if experiment is None:
            raise typer.BadParameter("experiment does not exist")
        start_experiment(session, experiment)
        console.print(f"Queued {experiment.id}")


@demo_app.command("verified-loop")
def run_verified_demo(
    compression_level: int = typer.Option(1, "--compression-level", min=1, max=9),
    chunk_size: int = typer.Option(65536, "--chunk-size"),
    repeats: int = typer.Option(3, "--repeats", min=2, max=100),
    minimum_improvement: float = typer.Option(0.05, "--minimum-improvement", min=0),
    maximum_ratio_regression: float = typer.Option(0.15, "--maximum-ratio-regression", min=0),
    samples: int = typer.Option(12, "--samples", min=3, max=10000),
    size_kib: int = typer.Option(512, "--size-kib", min=128, max=65536),
    workspace: Path = typer.Option(Path(".looper/verified-action"), "--workspace", file_okay=False),
) -> None:
    """Run a real local test -> change -> retest -> keep/rollback loop."""

    try:
        result = run_verified_compression_loop(
            workspace,
            candidate={
                "compression_level": compression_level,
                "chunk_size": chunk_size,
            },
            policy=VerificationPolicy(
                repeats=repeats,
                minimum_improvement_ratio=minimum_improvement,
                maximum_secondary_regression_ratio=maximum_ratio_regression,
                confidence_level=0.95,
                bootstrap_resamples=1000,
                random_seed=20260822,
            ),
            samples=samples,
            size_kib=size_kib,
        )
    except (OSError, RuntimeError, ValueError) as error:
        error_console.print(f"[red]verified action loop failed:[/red] {error}")
        raise typer.Exit(code=2) from error
    console.print_json(json.dumps(result))
    if result["decision"] == "failed":
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
