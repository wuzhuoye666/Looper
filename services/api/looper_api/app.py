from __future__ import annotations

import asyncio
import hmac
import io
import ipaddress
import json
import logging
import threading
import time
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from looper_core.canonical import canonical_digest, utc_now
from looper_core.cas import FileSystemCAS
from looper_core.contracts import (
    Aggregation,
    BenchmarkInputBinding,
    BudgetSpec,
    Comparison,
    Direction,
    ExperimentalDesign,
    ExperimentCreate,
    ExperimentMode,
    ExperimentSpec,
    ObjectiveSpec,
    PriceSnapshot,
    ScenarioBenchmarkSpec,
    SelectionDesign,
    TargetBindingSpec,
)
from looper_core.state import InvalidTransition
from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.analysis_service import build_analysis_snapshot
from looper_api.benchmark_compatibility import (
    BenchmarkTargetCompatibilityError,
    assert_target_compatible,
    incompatibility_summary,
    require_single_node_contract,
    requirement_summary,
    target_compatibility,
    target_environment,
)
from looper_api.benchmark_defaults import benchmark_selection_defaults
from looper_api.benchmark_packages import (
    MAX_PACKAGE_BYTES,
    BenchmarkPackageError,
    install_benchmark_package,
    parse_benchmark_package,
)
from looper_api.benchmark_registration import (
    BenchmarkRegistrationDraft,
    BenchmarkRegistrationRegister,
    BenchmarkRegistrationUpdate,
    RegistrationError,
    create_registration,
    draft_from_manifest_bytes,
    get_registration,
    register_benchmark,
    registration_view,
    selection_scenario_document,
    update_registration,
)
from looper_api.benchmark_runs import BenchmarkSmokeRunRequest, create_benchmark_smoke_run
from looper_api.capacity import (
    CapacityBuildRepairRequest,
    CapacityCreateRequest,
    CapacityDraftUpdate,
    CapacityError,
    CapacityStartRequest,
    cancel_capacity_study,
    capacity_view,
    create_capacity_study,
    list_capacity_studies,
    preflight_capacity_study,
    reconcile_capacity_studies,
    recover_interrupted_capacity_studies,
    repair_capacity_build_plan,
    retry_capacity_cleanup,
    start_capacity_study,
    update_capacity_study,
)
from looper_api.cloud_contracts import (
    CatalogFilters,
    InstanceNetworkResolveRequest,
    InstanceSelectionClass,
    InstanceTypeInfo,
    OrderPrepareRequest,
    OrderResolveRequest,
    ProviderId,
    QuoteCreateRequest,
    TargetDestroyRequest,
)
from looper_api.cloud_service import (
    CloudWorkflowError,
    _default_cloud_ssh_credentials,
    catalog_inventory,
    catalog_search,
    create_quote,
    delete_order,
    destroy_target,
    destroy_target_preview,
    ensure_managed_security_group,
    get_order,
    get_order_evidence,
    get_order_reconciliation_context,
    get_quote,
    global_search,
    list_order_events,
    list_orders,
    operator_auth_required,
    operator_token_ready,
    provider_views,
    purchase_quote,
    purchase_readiness,
    recover_interrupted_orders,
    resolve_instance_network,
    resolve_unknown_order,
    retry_pending_cloud_ssh,
)
from looper_api.config import Settings, get_settings
from looper_api.database import SessionLocal, get_session, init_database
from looper_api.deepseek_credentials import (
    DeepSeekCredentialError,
    EncryptedDeepSeekCredentialStore,
    effective_deepseek_key,
    effective_deepseek_settings,
)
from looper_api.evidence import build_evidence_bundle, verify_evidence_bundle
from looper_api.external_targets import (
    ConnectExternalTargetRequest,
    ExternalTargetError,
    ImportExternalTargetRequest,
    connect_existing_target,
    connect_external_target,
    import_external_target,
)
from looper_api.models import (
    ArtifactLinkRecord,
    ArtifactRecord,
    AttemptRecord,
    BenchmarkRecord,
    BenchmarkRegistrationRecord,
    CapacityStudyRecord,
    EventRecord,
    ExperimentRecord,
    SourceDiscoveryRecord,
    TargetRecord,
)
from looper_api.post_optimization import post_optimization_view, start_post_optimization
from looper_api.providers.alibaba_ecs import AlibabaInventoryError, sync_ecs_inventory
from looper_api.providers.base import CloudProviderError
from looper_api.providers.registry import CloudProviderRegistry, get_provider_registry
from looper_api.providers.tencent_cvm import TencentInventoryError, sync_cvm_inventory
from looper_api.remote_credentials import EncryptedSshCredentialStore, RemoteCredentialError
from looper_api.remote_recovery import (
    REMOTE_TARGET_PROVIDERS,
    RemoteWorkerRecoveryError,
    ensure_target_worker,
    remembered_target_ids,
    remembered_target_request,
    target_worker_ready,
)
from looper_api.remote_worker import deploy_remote_worker, deployment_status
from looper_api.retired_benchmarks import RETIRED_BENCHMARK_IDS, is_retired_benchmark
from looper_api.scheduler import (
    SchedulerError,
    cancel_experiment,
    create_experiment,
    pause_experiment,
    resume_experiment,
    retry_attempt,
    start_experiment,
)
from looper_api.seed import seed_system
from looper_api.selection_advisor import SelectionAdvisorRequest, advise_instance_types
from looper_api.selection_pricing import (
    PriceInfo,
    SelectionPriceQuoteRequest,
    resolve_item_price,
    selection_instance_quote,
)
from looper_api.price_catalog import AlibabaPriceTable, build_alibaba_region_map
from looper_api.serialization import (
    analysis_view,
    benchmark_view,
    dashboard_view,
    experiment_view,
    target_view,
)
from looper_api.source_archive_store import SourceArchiveError
from looper_api.source_discovery import (
    SourceDiscoveryError,
    create_discovery,
    discovery_view,
    list_discoveries,
    purge_expired_archives,
    recover_interrupted_discoveries,
    replace_retained_archive,
)
from looper_api.system_optimization import recover_interrupted_system_optimization_studies
from looper_api.system_optimization_api import router as system_optimization_router
from looper_api.system_optimization_runtime import reconcile_system_optimization_studies
from looper_api.variability_service import build_variability_report
from looper_api.worker_protocol import (
    ArtifactMetadata,
    AttemptCompletion,
    AttemptHeartbeat,
    AttemptStart,
    WorkerClaim,
    WorkerRegister,
)
from looper_api.worker_service import (
    WorkerError,
    authenticate_worker,
    claim_attempt,
    complete_attempt,
    expire_stale_leases,
    expire_stale_workers,
    heartbeat_attempt,
    register_worker,
    start_attempt,
    store_artifact,
)

SessionDependency = Annotated[Session, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]
ProviderRegistryDependency = Annotated[CloudProviderRegistry, Depends(get_provider_registry)]
WorkerToken = Annotated[str, Header(alias="X-Worker-Token")]
operator_bearer = HTTPBearer(auto_error=False)
OperatorCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(operator_bearer)]


class DeepSeekCredentialUpdate(BaseModel):
    apiKey: SecretStr


def _operator_authenticated(
    credentials: HTTPAuthorizationCredentials | None, app_settings: Settings
) -> bool:
    return bool(
        credentials
        and credentials.scheme.casefold() == "bearer"
        and operator_token_ready(app_settings)
        and hmac.compare_digest(credentials.credentials, app_settings.operator_token)
    )


def require_operator(
    credentials: OperatorCredentials,
    app_settings: SettingsDependency,
) -> str:
    if not operator_auth_required(app_settings):
        return "local-readonly"
    if not operator_token_ready(app_settings):
        raise CloudWorkflowError(
            "operator authentication is not configured; set a token of at least 32 characters",
            status_code=503,
            code="operator_auth_not_configured",
        )
    if not _operator_authenticated(credentials, app_settings):
        raise CloudWorkflowError(
            "valid operator bearer token required",
            status_code=401,
            code="operator_auth_required",
        )
    return "operator"


OperatorDependency = Annotated[str, Depends(require_operator)]


def require_configured_operator(
    credentials: OperatorCredentials,
    app_settings: SettingsDependency,
) -> str:
    if not operator_token_ready(app_settings):
        raise CloudWorkflowError(
            "operator authentication must be configured before managing provider credentials",
            status_code=503,
            code="operator_auth_not_configured",
        )
    if not _operator_authenticated(credentials, app_settings):
        raise CloudWorkflowError(
            "valid operator bearer token required",
            status_code=401,
            code="operator_auth_required",
        )
    return "operator"


ConfiguredOperatorDependency = Annotated[str, Depends(require_configured_operator)]
logger = logging.getLogger(__name__)


async def _lease_sweeper() -> None:
    while True:
        await asyncio.sleep(5)
        with SessionLocal() as session:
            try:
                expire_stale_leases(session)
                expire_stale_workers(session, get_settings())
                purge_expired_archives(session, get_settings())
                session.commit()
            except Exception:
                session.rollback()
        reconcile_capacity_studies(get_settings())


async def _system_optimization_reconciler() -> None:
    while True:
        await asyncio.sleep(5)
        try:
            await asyncio.to_thread(reconcile_system_optimization_studies, get_settings())
        except Exception:
            logger.exception("System optimization reconciliation failed")


async def _remote_worker_recovery() -> None:
    settings = get_settings()
    retry_after: dict[str, float] = {}
    failures: dict[str, int] = {}

    async def run_recovery_call(function: Any, *args: Any, **kwargs: Any) -> Any:
        """Run SSH recovery in a daemon thread so API shutdown never waits on it."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def deliver(value: Any = None, error: BaseException | None = None) -> None:
            if future.cancelled() or future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)

        def invoke() -> None:
            try:
                value = function(*args, **kwargs)
            except BaseException as error:
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(deliver, None, error)
            else:
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(deliver, value, None)

        threading.Thread(
            target=invoke,
            daemon=True,
            name=f"looper-recovery-{getattr(function, '__name__', 'call')}",
        ).start()
        return await future

    async def recover_target(target_id: str) -> None:
        try:
            ready = await run_recovery_call(target_worker_ready, target_id, settings)
            if ready:
                failures.pop(target_id, None)
                retry_after.pop(target_id, None)
                return
            await run_recovery_call(
                ensure_target_worker,
                target_id,
                settings,
                registration_timeout=30.0,
            )
            failures.pop(target_id, None)
            retry_after.pop(target_id, None)
        except Exception:
            logger.exception("Remote Worker recovery failed for %s", target_id)
            count = failures.get(target_id, 0) + 1
            failures[target_id] = count
            retry_after[target_id] = time.monotonic() + min(
                300.0, 15.0 * (2 ** (count - 1))
            )

    while True:
        try:
            targets = set(await run_recovery_call(remembered_target_ids, settings))
        except Exception:
            logger.exception("Unable to read remembered remote Worker credentials")
            await asyncio.sleep(30)
            continue
        due = [
            target_id
            for target_id in sorted(targets)
            if retry_after.get(target_id, 0.0) <= time.monotonic()
        ]
        if due:
            await asyncio.gather(*(recover_target(target_id) for target_id in due))
        await asyncio.sleep(5)


def _retry_pending_cloud_ssh_sync(settings: Settings) -> int:
    with SessionLocal() as session:
        connected = retry_pending_cloud_ssh(session, settings)
        session.commit()
        return connected


async def _cloud_ssh_recovery() -> None:
    settings = get_settings()
    while True:
        try:
            await asyncio.to_thread(_retry_pending_cloud_ssh_sync, settings)
        except Exception:
            logger.exception("Pending cloud SSH recovery failed")
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_directories()
    init_database()
    with SessionLocal() as session:
        recover_interrupted_orders(session)
        recover_interrupted_discoveries(session)
        recover_interrupted_capacity_studies(session)
        recover_interrupted_system_optimization_studies(session)
        purge_expired_archives(session, settings)
        seed_system(session)
        session.commit()
    sweeper = asyncio.create_task(_lease_sweeper())
    system_optimization_reconciler = asyncio.create_task(
        _system_optimization_reconciler()
    )
    remote_recovery = asyncio.create_task(_remote_worker_recovery())
    cloud_ssh_recovery = asyncio.create_task(_cloud_ssh_recovery())
    try:
        yield
    finally:
        sweeper.cancel()
        system_optimization_reconciler.cancel()
        remote_recovery.cancel()
        cloud_ssh_recovery.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        with suppress(asyncio.CancelledError):
            await system_optimization_reconciler
        with suppress(asyncio.CancelledError):
            await remote_recovery
        with suppress(asyncio.CancelledError):
            await cloud_ssh_recovery


app = FastAPI(
    title="Looper API",
    version="0.1.0",
    description="Auditable closed-loop systems performance optimization control plane.",
    lifespan=lifespan,
)
# System optimization routes are included after all application routers.
app.include_router(system_optimization_router)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Accept",
        "Authorization",
        "X-Worker-Token",
        "Idempotency-Key",
    ],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)


@app.middleware("http")
async def origin_guard(request: Request, call_next: Any) -> Any:
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin not in settings.origin_list:
            return JSONResponse(status_code=403, content={"message": "origin is not allowed"})
    return await call_next(request)


@app.exception_handler(SchedulerError)
@app.exception_handler(BenchmarkTargetCompatibilityError)
@app.exception_handler(WorkerError)
@app.exception_handler(InvalidTransition)
@app.exception_handler(TencentInventoryError)
@app.exception_handler(AlibabaInventoryError)
@app.exception_handler(CloudProviderError)
@app.exception_handler(CloudWorkflowError)
@app.exception_handler(RegistrationError)
@app.exception_handler(ExternalTargetError)
@app.exception_handler(SourceDiscoveryError)
@app.exception_handler(DeepSeekCredentialError)
@app.exception_handler(RemoteCredentialError)
@app.exception_handler(RemoteWorkerRecoveryError)
@app.exception_handler(SourceArchiveError)
@app.exception_handler(CapacityError)
async def domain_error(_request: Request, error: Exception) -> JSONResponse:
    status_code = getattr(error, "status_code", 409)
    code = getattr(error, "code", error.__class__.__name__)
    content: dict[str, Any] = {"message": str(error), "code": code}
    constraints = getattr(error, "constraints", None)
    if constraints is not None:
        content["constraints"] = constraints
    return JSONResponse(status_code=status_code, content=content)


@app.exception_handler(ValidationError)
async def validation_error(_request: Request, error: ValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"message": "request validation failed", "details": error.errors()},
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "Looper", "version": "0.1.0", "docs": "/docs"}


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "database": "ready", "artifact_store": "ready"}


@app.get("/api/v1/benchmark-skills/looper-benchmark-configure")
def download_benchmark_configure_skill() -> StreamingResponse:
    archive = build_benchmark_configure_skill_archive()
    return StreamingResponse(
        iter([archive]),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="looper-benchmark-configure.zip"',
            "Content-Length": str(len(archive)),
        },
    )


def build_benchmark_configure_skill_archive() -> bytes:
    skill_root = files("looper_api").joinpath("assets", "skills", "looper-benchmark-configure")
    members = (
        "SKILL.md",
        "agents/openai.yaml",
        "references/benchmark-interface.md",
        "templates/infrastructure-multi-node.yaml",
        "templates/infrastructure-single-node.yaml",
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            resource = skill_root.joinpath(*member.split("/"))
            if not resource.is_file():
                raise HTTPException(
                    status_code=404,
                    detail="benchmark configuration skill is unavailable",
                )
            info = zipfile.ZipInfo(
                f"looper-benchmark-configure/{member}",
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, resource.read_bytes())
    return buffer.getvalue()


@app.get("/api/v1/dashboard")
def dashboard(session: SessionDependency) -> dict[str, Any]:
    return dashboard_view(session)


@app.get("/api/v1/search")
def search_all(
    session: SessionDependency,
    _operator: OperatorDependency,
    q: str = Query(min_length=1, max_length=120),
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    items = global_search(session, q, limit)
    return {"items": items, "total": len(items), "query": q}


@app.get("/api/v1/cloud/providers")
def cloud_providers(
    app_settings: SettingsDependency,
    registry: ProviderRegistryDependency,
) -> dict[str, Any]:
    return {"items": provider_views(app_settings, registry)}


@app.get("/api/v1/cloud/purchase-readiness")
def cloud_purchase_readiness(
    app_settings: SettingsDependency,
    registry: ProviderRegistryDependency,
) -> dict[str, Any]:
    return purchase_readiness(app_settings, registry)


@app.get("/api/v1/cloud/ssh-defaults")
def cloud_ssh_defaults(
    app_settings: SettingsDependency,
    _operator: ConfiguredOperatorDependency,
) -> JSONResponse:
    password = (
        app_settings.default_ssh_password.get_secret_value()
        if app_settings.default_ssh_password
        else ""
    )
    key_path = Path(app_settings.default_ssh_private_key_path).expanduser()
    return JSONResponse(
        {
            "username": app_settings.default_ssh_username.strip() or "root",
            "port": app_settings.default_ssh_port,
            "authMethod": app_settings.default_ssh_auth_method,
            "password": password,
            "passwordConfigured": bool(password),
            "privateKeyConfigured": bool(
                app_settings.default_ssh_private_key_path and key_path.is_file()
            ),
        },
        headers={"Cache-Control": "no-store, private", "Pragma": "no-cache"},
    )


def operator_session_status(
    credentials: OperatorCredentials,
    app_settings: SettingsDependency,
    *,
    local_bootstrap_available: bool = False,
) -> dict[str, bool]:
    return {
        "required": operator_auth_required(app_settings),
        "configured": operator_token_ready(app_settings),
        "authenticated": _operator_authenticated(credentials, app_settings),
        "operatorGateReady": operator_token_ready(app_settings),
        "localBootstrapAvailable": local_bootstrap_available,
    }


def local_operator_bootstrap_available(request: Request, app_settings: Settings) -> bool:
    if app_settings.host.strip().casefold() not in {"127.0.0.1", "::1", "localhost"}:
        return False
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return request.client.host.casefold() == "localhost"


@app.get("/api/v1/operator/session")
def operator_session(
    request: Request,
    credentials: OperatorCredentials,
    app_settings: SettingsDependency,
) -> dict[str, bool]:
    return operator_session_status(
        credentials,
        app_settings,
        local_bootstrap_available=local_operator_bootstrap_available(request, app_settings),
    )


@app.post("/api/v1/operator/local-session")
def create_local_operator_session(
    request: Request,
    app_settings: SettingsDependency,
) -> dict[str, str]:
    if not local_operator_bootstrap_available(request, app_settings):
        raise CloudWorkflowError(
            "local operator bootstrap is available only on a loopback-bound control plane",
            status_code=403,
            code="local_operator_bootstrap_forbidden",
        )
    if not operator_token_ready(app_settings):
        raise CloudWorkflowError(
            "operator authentication is not configured",
            status_code=503,
            code="operator_auth_not_configured",
        )
    return {"token": app_settings.operator_token}


@app.get("/api/v1/source-discoveries/readiness")
def source_discovery_readiness(app_settings: SettingsDependency) -> dict[str, Any]:
    api_key, _source = effective_deepseek_key(app_settings)
    configured = bool(api_key)
    return {
        "configured": configured,
        "provider": "deepseek",
        "model": app_settings.deepseek_model,
        "baseUrl": str(app_settings.deepseek_base_url).rstrip("/"),
        "maxArchiveBytes": app_settings.source_discovery_max_archive_bytes,
        "acceptedMediaTypes": ["application/zip"],
        "requiredEnvironment": [] if configured else ["LOOPER_DEEPSEEK_API_KEY"],
        "dataDisclosure": (
            "Readable, non-sensitive source snippets selected by the harness are sent "
            "to the configured DeepSeek endpoint."
        ),
    }


def deepseek_provider_config_view(app_settings: Settings) -> dict[str, Any]:
    api_key, source = effective_deepseek_key(app_settings)
    return {
        "configured": bool(api_key),
        "source": source,
        "maskedKey": f"••••••••{api_key[-4:]}" if api_key else None,
        "provider": "deepseek",
        "model": app_settings.deepseek_model,
        "baseUrl": str(app_settings.deepseek_base_url).rstrip("/"),
        "encryptedAtRest": source == "stored",
    }


@app.get("/api/v1/source-discoveries/provider-config")
def deepseek_provider_config(
    app_settings: SettingsDependency,
    _operator: ConfiguredOperatorDependency,
) -> dict[str, Any]:
    return deepseek_provider_config_view(app_settings)


@app.put("/api/v1/source-discoveries/provider-config")
def update_deepseek_provider_config(
    payload: DeepSeekCredentialUpdate,
    app_settings: SettingsDependency,
    _operator: ConfiguredOperatorDependency,
) -> dict[str, Any]:
    EncryptedDeepSeekCredentialStore(app_settings).save(payload.apiKey.get_secret_value())
    return deepseek_provider_config_view(app_settings)


@app.delete("/api/v1/source-discoveries/provider-config")
def delete_deepseek_provider_config(
    app_settings: SettingsDependency,
    _operator: ConfiguredOperatorDependency,
) -> dict[str, Any]:
    EncryptedDeepSeekCredentialStore(app_settings).delete()
    return deepseek_provider_config_view(app_settings)


@app.get("/api/v1/source-discoveries")
def source_discovery_history(
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    purge_expired_archives(session, app_settings)
    session.commit()
    items = [
        discovery_view(record, app_settings) for record in list_discoveries(session, limit)
    ]
    return {"items": items, "total": len(items)}


@app.get("/api/v1/source-discoveries/{discovery_id}")
def source_discovery_detail(
    discovery_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(SourceDiscoveryRecord, discovery_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source discovery not found")
    purge_expired_archives(session, app_settings)
    session.commit()
    return discovery_view(record, app_settings)


@app.post("/api/v1/source-discoveries", status_code=201)
async def discover_source_interfaces(
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
    archive: UploadFile = File(...),
) -> dict[str, Any]:
    content_type = (archive.content_type or "").casefold()
    if not archive.filename or not archive.filename.casefold().endswith(".zip"):
        raise SourceDiscoveryError(
            "source archive filename must end with .zip", code="invalid_archive_type"
        )
    if content_type and content_type not in {
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    }:
        raise SourceDiscoveryError(
            "only ZIP source archives are accepted", code="invalid_archive_type"
        )
    payload = await archive.read(app_settings.source_discovery_max_archive_bytes + 1)
    record = await create_discovery(
        session, archive.filename, payload, effective_deepseek_settings(app_settings)
    )
    return discovery_view(record, app_settings)


@app.put("/api/v1/source-discoveries/{discovery_id}/archive")
async def replace_source_discovery_archive(
    discovery_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
    archive: UploadFile = File(...),
) -> dict[str, Any]:
    record = session.get(SourceDiscoveryRecord, discovery_id)
    if record is None:
        raise HTTPException(status_code=404, detail="source discovery not found")
    if record.status != "completed":
        raise SourceArchiveError("only a completed discovery can retain a source archive")
    if not archive.filename or not archive.filename.casefold().endswith(".zip"):
        raise SourceArchiveError("source archive filename must end with .zip")
    payload = await archive.read(app_settings.source_discovery_max_archive_bytes + 1)
    replace_retained_archive(session, record, payload, app_settings)
    session.commit()
    return discovery_view(record, app_settings)


@app.post("/api/v1/source-discoveries/{discovery_id}/capacity-studies", status_code=201)
async def create_source_capacity_study(
    discovery_id: str,
    request: CapacityCreateRequest,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    discovery = session.get(SourceDiscoveryRecord, discovery_id)
    if discovery is None:
        raise HTTPException(status_code=404, detail="source discovery not found")
    record = await create_capacity_study(
        session,
        discovery,
        effective_deepseek_settings(app_settings),
        name=request.name,
    )
    session.commit()
    return capacity_view(session, record, app_settings)


@app.get("/api/v1/capacity-studies")
def capacity_study_history(
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    items = [
        capacity_view(session, item, app_settings)
        for item in list_capacity_studies(session, limit)
    ]
    return {"items": items, "total": len(items)}


@app.get("/api/v1/capacity-studies/{study_id}")
def capacity_study_detail(
    study_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(CapacityStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="capacity study not found")
    return capacity_view(session, record, app_settings)


@app.patch("/api/v1/capacity-studies/{study_id}")
def update_capacity_study_draft(
    study_id: str,
    request: CapacityDraftUpdate,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(CapacityStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="capacity study not found")
    update_capacity_study(session, record, request)
    session.commit()
    return capacity_view(session, record, app_settings)


@app.post("/api/v1/capacity-studies/{study_id}/build/repair")
async def repair_capacity_study_build(
    study_id: str,
    request: CapacityBuildRepairRequest,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(CapacityStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="capacity study not found")
    await repair_capacity_build_plan(
        session,
        record,
        request,
        effective_deepseek_settings(app_settings),
    )
    session.commit()
    return capacity_view(session, record, app_settings)


@app.post("/api/v1/capacity-studies/{study_id}/preflight")
def run_capacity_study_preflight(
    study_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(CapacityStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="capacity study not found")
    preflight_capacity_study(session, record, app_settings)
    session.commit()
    return capacity_view(session, record, app_settings)


@app.post("/api/v1/capacity-studies/{study_id}/start")
def start_capacity_study_run(
    study_id: str,
    request: CapacityStartRequest,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(CapacityStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="capacity study not found")
    purge_expired_archives(session, app_settings)
    session.commit()
    start_capacity_study(session, record, request, app_settings)
    session.commit()
    return capacity_view(session, record, app_settings)


@app.post("/api/v1/capacity-studies/{study_id}/cancel")
def cancel_capacity_study_run(
    study_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(CapacityStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="capacity study not found")
    cancel_capacity_study(session, record)
    session.commit()
    return capacity_view(session, record, app_settings)


@app.post("/api/v1/capacity-studies/{study_id}/cleanup-retry")
def retry_capacity_study_cleanup(
    study_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(CapacityStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="capacity study not found")
    retry_capacity_cleanup(session, record)
    session.commit()
    return capacity_view(session, record, app_settings)


@app.get("/api/v1/source-discoveries/{discovery_id}/contract")
def export_source_discovery_contract(
    discovery_id: str,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> JSONResponse:
    record = session.get(SourceDiscoveryRecord, discovery_id)
    if record is None or record.contract_json is None:
        raise HTTPException(status_code=404, detail="completed interface contract not found")
    return JSONResponse(
        content=record.contract_json,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{discovery_id}.interface-contract.json"'
        },
    )


@app.get("/api/v1/cloud/auth/status")
def cloud_auth_status(
    credentials: OperatorCredentials,
    app_settings: SettingsDependency,
) -> dict[str, bool]:
    return operator_session_status(credentials, app_settings)


@app.get("/api/v1/cloud/catalog/{provider}/{resource_type}")
def cloud_catalog(
    provider: ProviderId,
    resource_type: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    registry: ProviderRegistryDependency,
    credentials: OperatorCredentials,
    region: str | None = Query(default=None, max_length=64),
    zone: str | None = Query(default=None, max_length=64),
    vpc_id: str | None = Query(default=None, max_length=120),
    query: str | None = Query(default=None, max_length=120),
    architecture_class: InstanceSelectionClass | None = Query(default=None),
    type_kind: str | None = Query(default=None, max_length=60),
    family_token: str | None = Query(default=None, max_length=80),
    min_cpu: int | None = Query(default=None, ge=1, le=1024),
    max_cpu: int | None = Query(default=None, ge=1, le=1024),
    min_memory_gib: float | None = Query(default=None, ge=0.25, le=65536),
    max_memory_gib: float | None = Query(default=None, ge=0.25, le=65536),
    image_type: str | None = Query(default=None, max_length=60),
    platform: str | None = Query(default=None, max_length=80),
    instance_type: str | None = Query(default=None, max_length=120),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    account_resources = {"vpc", "subnet", "security-group", "key-pair"}
    if resource_type not in {
        "region",
        "zone",
        "instance-type",
        "image",
        *account_resources,
    }:
        raise HTTPException(status_code=404, detail="unsupported cloud catalog resource")
    if resource_type in account_resources:
        require_operator(credentials, app_settings)
    filters = CatalogFilters(
        region=region,
        zone=zone,
        vpcId=vpc_id,
        query=query,
        architectureClass=architecture_class,
        typeKind=type_kind,
        familyToken=family_token,
        minCpu=min_cpu,
        maxCpu=max_cpu,
        minMemoryGib=min_memory_gib,
        maxMemoryGib=max_memory_gib,
        imageType=image_type,
        platform=platform,
        instanceType=instance_type,
        offset=offset,
        limit=limit,
    )
    result = catalog_search(
        session,
        app_settings,
        registry,
        provider,
        resource_type,  # type: ignore[arg-type]
        filters,
    )
    session.commit()
    return result.model_dump(mode="json", by_alias=True)


_PRICE_TABLE: AlibabaPriceTable | None = None
_PRICE_TABLE_PATH = Path(__file__).resolve().parents[3] / "prices" / "instancePrice.csv"


def _get_price_table() -> AlibabaPriceTable | None:
    global _PRICE_TABLE
    if _PRICE_TABLE is not None:
        return _PRICE_TABLE
    if not _PRICE_TABLE_PATH.is_file():
        return None
    provider = get_provider_registry().get(ProviderId.ALIBABA)
    region_map = build_alibaba_region_map(provider)
    _PRICE_TABLE = AlibabaPriceTable(_PRICE_TABLE_PATH, region_map)
    return _PRICE_TABLE


@app.post("/api/v1/cloud/selection-advisor/search")
def cloud_selection_advisor(
    request: SelectionAdvisorRequest,
    session: SessionDependency,
    app_settings: SettingsDependency,
    registry: ProviderRegistryDependency,
) -> dict[str, Any]:
    catalog = catalog_inventory(
        session,
        app_settings,
        registry,
        ProviderId(request.provider),
        "instance-type",
        CatalogFilters(region=request.region, zone=request.zone, offset=request.offset),
    )
    price_table = _get_price_table()

    def price_reader(item: InstanceTypeInfo) -> "PriceInfo | None":
        return resolve_item_price(
            item,
            registry=registry,
            price_table=price_table,
        )

    response = advise_instance_types(
        request,
        [InstanceTypeInfo.model_validate(item) for item in catalog.items],
        source=catalog.source,
        fetched_at=catalog.fetched_at.isoformat(),
        expires_at=catalog.expires_at.isoformat(),
        stale=catalog.stale,
        warning=catalog.warning,
        price_reader=price_reader,
    )
    session.commit()
    return response.model_dump(mode="json", by_alias=True)


@app.post("/api/v1/cloud/selection-advisor/quote")
def cloud_selection_advisor_quote(
    request: SelectionPriceQuoteRequest,
    registry: ProviderRegistryDependency,
) -> dict[str, Any]:
    result = selection_instance_quote(request, registry)
    return result.model_dump(mode="json", by_alias=True)


@app.post("/api/v1/cloud/network/{provider}/managed-security-group", status_code=201)
def cloud_managed_security_group(
    provider: ProviderId,
    session: SessionDependency,
    registry: ProviderRegistryDependency,
    _operator: OperatorDependency,
    region: str = Query(min_length=2, max_length=64),
    vpc_id: str | None = Query(default=None, min_length=1, max_length=120),
) -> dict[str, Any]:
    result = ensure_managed_security_group(
        session,
        registry,
        provider,
        region,
        vpc_id=vpc_id,
    )
    session.commit()
    return result


@app.post("/api/v1/cloud/network/{provider}/resolve-instance-network")
def cloud_resolve_instance_network(
    provider: ProviderId,
    request: InstanceNetworkResolveRequest,
    session: SessionDependency,
    app_settings: SettingsDependency,
    registry: ProviderRegistryDependency,
    _operator: OperatorDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> dict[str, Any]:
    result = resolve_instance_network(
        session,
        app_settings,
        registry,
        provider,
        request,
        idempotency_key=idempotency_key,
    )
    session.commit()
    return result.model_dump(mode="json", by_alias=True)


@app.post("/api/v1/cloud/quotes", status_code=201)
def cloud_quote(
    request: QuoteCreateRequest,
    session: SessionDependency,
    app_settings: SettingsDependency,
    registry: ProviderRegistryDependency,
    _operator: OperatorDependency,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, Any]:
    result = create_quote(
        session,
        app_settings,
        registry,
        request.spec,
        idempotency_key,
    )
    session.commit()
    return result


@app.get("/api/v1/cloud/quotes/{quote_id}")
def cloud_quote_get(
    quote_id: str,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    return get_quote(session, quote_id)


@app.post("/api/v1/cloud/orders/purchase", status_code=201)
def cloud_order_purchase(
    request: OrderPrepareRequest,
    session: SessionDependency,
    app_settings: SettingsDependency,
    registry: ProviderRegistryDependency,
    _operator: OperatorDependency,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, Any]:
    return purchase_quote(
        session,
        app_settings,
        registry,
        request.quote_id,
        idempotency_key,
        request.ssh_credentials,
        request.ssh_auth_method,
        request.ssh_password.get_secret_value() if request.ssh_password else None,
        request.remember_credentials,
    )


@app.get("/api/v1/cloud/orders")
def cloud_order_list(
    session: SessionDependency,
    _operator: OperatorDependency,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    items = list_orders(session, status=status, limit=limit)
    return {"items": items, "total": len(items)}


@app.get("/api/v1/cloud/orders/{order_id}")
def cloud_order_get(
    order_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    return get_order(session, app_settings, order_id)


@app.delete("/api/v1/cloud/orders/{order_id}", status_code=204)
def cloud_order_delete(
    order_id: str,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> None:
    delete_order(session, order_id)


@app.get("/api/v1/cloud/orders/{order_id}/events")
def cloud_order_events(
    order_id: str,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    items = list_order_events(session, order_id)
    return {"items": items, "total": len(items)}


@app.get("/api/v1/cloud/orders/{order_id}/reconciliation-context")
def cloud_order_reconciliation_context(
    order_id: str,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    return get_order_reconciliation_context(session, order_id)


@app.get("/api/v1/cloud/orders/{order_id}/evidence")
def cloud_order_evidence(
    order_id: str,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    return get_order_evidence(session, order_id)


@app.post("/api/v1/cloud/orders/{order_id}/resolve")
def cloud_order_resolve(
    order_id: str,
    request: OrderResolveRequest,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    return resolve_unknown_order(session, order_id, request)


@app.get("/api/v1/benchmarks")
def list_benchmarks(session: SessionDependency) -> dict[str, Any]:
    internal_benchmark_ids = {
        "looper.fixture.config-driven",
        "looper.demo.compression",
    }
    records = list(
        session.scalars(
            select(BenchmarkRecord).where(
                BenchmarkRecord.benchmark_id.not_in(
                    internal_benchmark_ids | RETIRED_BENCHMARK_IDS
                )
            ).order_by(
                BenchmarkRecord.benchmark_id,
                BenchmarkRecord.installed_at.desc(),
                BenchmarkRecord.key.desc(),
            )
        )
    )
    # Versions remain addressable for historical experiments, but the catalog
    # exposes one current package per stable Benchmark ID. A newly registered
    # version therefore replaces the old choice instead of creating duplicates.
    current_by_id: dict[str, BenchmarkRecord] = {}
    for record in records:
        current_by_id.setdefault(record.benchmark_id, record)
    current = sorted(current_by_id.values(), key=lambda item: item.name.casefold())
    registrations = {
        item.benchmark_key: item
        for item in session.scalars(
            select(BenchmarkRegistrationRecord).where(
                BenchmarkRegistrationRecord.benchmark_key.is_not(None)
            )
        )
    }
    return {
        "items": [benchmark_view(item, registrations.get(item.key)) for item in current],
        "total": len(current),
    }


@app.get("/api/v1/benchmarks/{benchmark_id}/versions/{version}/target-options")
def benchmark_target_options(
    benchmark_id: str,
    version: str,
    session: SessionDependency,
) -> dict[str, Any]:
    if is_retired_benchmark(benchmark_id):
        raise HTTPException(status_code=404, detail="benchmark version not found")
    benchmark = session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == benchmark_id,
            BenchmarkRecord.version == version,
        )
    )
    if benchmark is None:
        raise HTTPException(status_code=404, detail="benchmark version not found")
    current = session.scalar(
        select(BenchmarkRecord)
        .where(BenchmarkRecord.benchmark_id == benchmark_id)
        .order_by(BenchmarkRecord.installed_at.desc(), BenchmarkRecord.key.desc())
        .limit(1)
    )
    if current is None or current.key != benchmark.key:
        raise BenchmarkTargetCompatibilityError(
            "Benchmark 版本已被替换，请重新选择当前版本",
            [{
                "code": "benchmark_version_replaced",
                "field": "benchmark.version",
                "required": current.version if current else None,
                "actual": version,
                "message": "只能为当前目录版本选择资源",
            }],
        )
    registration = session.scalar(
        select(BenchmarkRegistrationRecord).where(
            BenchmarkRegistrationRecord.benchmark_key == benchmark.key,
            BenchmarkRegistrationRecord.status == "registered",
        )
    )
    view = benchmark_view(benchmark, registration)
    if not view["selectionReady"]:
        raise BenchmarkTargetCompatibilityError(
            "Benchmark 当前不可用于选型研究",
            [{
                "code": "benchmark_not_selection_ready",
                "field": "benchmark.selectionReady",
                "required": True,
                "actual": False,
                "message": "Benchmark 尚未具备可信且可执行的选型包",
            }],
        )
    node_group = require_single_node_contract(benchmark.manifest_json)
    compatible_by_environment: dict[str, dict[str, Any]] = {}
    rejected: list[list[dict[str, Any]]] = []
    targets = list(
        session.scalars(
            select(TargetRecord)
            .where(TargetRecord.lifecycle_status == "active")
            .order_by(TargetRecord.provider, TargetRecord.name, TargetRecord.id)
        )
    )
    for target in targets:
        constraints = target_compatibility(benchmark.manifest_json, target)
        if constraints:
            rejected.append(constraints)
            continue
        environment_id, label = target_environment(target)
        environment = compatible_by_environment.setdefault(
            environment_id,
            {"id": environment_id, "label": label, "targets": []},
        )
        environment["targets"].append(target_view(target))
    environments = sorted(
        (
            {**environment, "compatibleCount": len(environment["targets"])}
            for environment in compatible_by_environment.values()
        ),
        key=lambda item: (item["label"], item["id"]),
    )
    scenario = benchmark.manifest_json["spec"].get("scenario") or {}
    return {
        "benchmarkId": benchmark.benchmark_id,
        "version": benchmark.version,
        "topology": scenario.get("topology"),
        "machineCount": 1,
        "nodeGroup": {
            "id": node_group["id"],
            "role": node_group["role"],
            "requirements": node_group.get("requirements") or {},
            "summary": requirement_summary(benchmark.manifest_json),
        },
        "environments": environments,
        "rejectedSummary": incompatibility_summary(rejected),
    }


@app.post(
    "/api/v1/benchmarks/{benchmark_id}/versions/{version}/smoke-runs",
    status_code=202,
)
def create_benchmark_smoke_run_endpoint(
    benchmark_id: str,
    version: str,
    request: BenchmarkSmokeRunRequest,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    experiment = create_benchmark_smoke_run(
        session,
        benchmark_id,
        version,
        request,
    )
    session.commit()
    return experiment_view(session, experiment, detail=True)


@app.get("/api/v1/benchmark-registrations")
def list_benchmark_registrations(
    session: SessionDependency,
    _operator: OperatorDependency,
    status: str | None = Query(default=None, pattern="^(draft|registered)$"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    statement = select(BenchmarkRegistrationRecord).order_by(
        BenchmarkRegistrationRecord.updated_at.desc()
    )
    if status:
        statement = statement.where(BenchmarkRegistrationRecord.status == status)
    records = list(session.scalars(statement.limit(limit)))
    return {"items": [registration_view(item) for item in records], "total": len(records)}


@app.post("/api/v1/benchmark-registrations", status_code=201)
def create_benchmark_registration(
    request: BenchmarkRegistrationDraft,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = create_registration(session, request)
    session.commit()
    return registration_view(record)


@app.post("/api/v1/benchmark-registrations/import", status_code=201)
async def import_benchmark_registration(
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
    configuration: UploadFile = File(...),
) -> dict[str, Any]:
    filename = configuration.filename or "benchmark.yaml"
    limit = MAX_PACKAGE_BYTES if filename.casefold().endswith(".zip") else 2 * 1024 * 1024
    raw = await configuration.read(limit + 1)
    package_digest: str | None = None
    package_path: str | None = None
    if filename.casefold().endswith(".zip"):
        try:
            package = parse_benchmark_package(raw)
        except BenchmarkPackageError as error:
            raise RegistrationError(
                str(error), status_code=422, code="invalid_benchmark_package"
            ) from error
        draft = draft_from_manifest_bytes(
            package.manifest_bytes,
            filename=package.manifest_name,
        )
        installed = install_benchmark_package(app_settings.data_dir, package)
        package_digest = package.package_digest
        package_path = str(installed)
    else:
        draft = draft_from_manifest_bytes(raw, filename=filename)
    record = create_registration(
        session,
        draft,
        package_digest=package_digest,
        package_path=package_path,
    )
    session.commit()
    return registration_view(record)


@app.get("/api/v1/benchmark-registrations/{registration_id}")
def get_benchmark_registration(
    registration_id: str,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    return registration_view(get_registration(session, registration_id))


@app.get("/api/v1/benchmark-registrations/{registration_id}/events")
def get_benchmark_registration_events(
    registration_id: str,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    get_registration(session, registration_id)
    records = list(
        session.scalars(
            select(EventRecord)
            .where(
                EventRecord.entity_type == "benchmark_registration",
                EventRecord.entity_id == registration_id,
            )
            .order_by(EventRecord.created_at, EventRecord.sequence)
        )
    )
    return {
        "items": [
            {
                "id": item.id,
                "eventType": item.event_type,
                "payload": item.payload_json,
                "createdAt": item.created_at.isoformat(),
            }
            for item in records
        ],
        "total": len(records),
    }


@app.put("/api/v1/benchmark-registrations/{registration_id}")
def update_benchmark_registration(
    registration_id: str,
    request: BenchmarkRegistrationUpdate,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = update_registration(session, registration_id, request)
    session.commit()
    return registration_view(record)


@app.post("/api/v1/benchmark-registrations/{registration_id}/register")
def finalize_benchmark_registration(
    registration_id: str,
    request: BenchmarkRegistrationRegister,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = register_benchmark(session, registration_id, request)
    session.commit()
    return registration_view(record)


@app.get("/api/v1/targets")
def list_targets(
    session: SessionDependency,
    app_settings: SettingsDependency,
    include_inactive: bool = Query(default=True),
) -> dict[str, Any]:
    expire_stale_workers(session, app_settings)
    statement = select(TargetRecord)
    if not include_inactive:
        statement = statement.where(TargetRecord.lifecycle_status == "active")
    records = list(session.scalars(statement.order_by(TargetRecord.name)))
    try:
        remembered = set(EncryptedSshCredentialStore(app_settings).target_ids())
    except RemoteCredentialError:
        # Inventory remains readable even if the local credential vault needs repair.
        remembered = set()
    items: list[dict[str, Any]] = []
    for record in records:
        item = target_view(record)
        item["credentialsRemembered"] = record.id in remembered
        item["deployment"] = deployment_status(record.id)
        items.append(item)
    return {"items": items, "total": len(records)}


@app.post("/api/v1/targets/tencent-cvm/sync")
def sync_tencent_targets(
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
    region: str = Query(default="ap-guangzhou", min_length=3, max_length=40),
    instance_id: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    records = sync_cvm_inventory(
        session,
        region,
        instance_ids=instance_id,
        credential_store=EncryptedSshCredentialStore(app_settings),
    )
    session.commit()
    return {"items": [target_view(item) for item in records], "total": len(records)}


def _sync_all_cloud_inventory(
    session: Session,
    registry: CloudProviderRegistry,
    credential_store: EncryptedSshCredentialStore,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, Any]:
    records_by_id: dict[str, TargetRecord] = {}
    synced_regions: dict[str, list[str]] = {}
    completed_regions: dict[str, list[str]] = {}
    errors: list[dict[str, Any]] = []
    syncers = {
        ProviderId.TENCENT: sync_cvm_inventory,
        ProviderId.ALIBABA: sync_ecs_inventory,
    }
    for provider_id, sync_inventory in syncers.items():
        completed_regions[provider_id.value] = []
        try:
            regions = sorted({item.id for item in registry.get(provider_id).list_regions()})
        except CloudProviderError as error:
            synced_regions[provider_id.value] = []
            errors.append(
                {
                    "provider": provider_id.value,
                    "region": None,
                    "code": error.code,
                    "message": str(error),
                }
            )
            continue
        synced_regions[provider_id.value] = regions
        for region in regions:
            try:
                records = sync_inventory(
                    session,
                    region,
                    credential_store=credential_store,
                )
            except CloudProviderError as error:
                session.rollback()
                errors.append(
                    {
                        "provider": provider_id.value,
                        "region": region,
                        "code": error.code,
                        "message": str(error),
                    }
                )
                continue
            records_by_id.update({record.id: record for record in records})
            completed_regions[provider_id.value].append(region)
            if checkpoint is not None:
                checkpoint()
    records = list(records_by_id.values())
    return {
        "items": [target_view(item) for item in records],
        "total": len(records),
        "regions": synced_regions,
        "completedRegions": completed_regions,
        "errors": errors,
    }


@app.post("/api/v1/targets/cloud/sync")
def sync_cloud_targets(
    session: SessionDependency,
    app_settings: SettingsDependency,
    registry: ProviderRegistryDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    result = _sync_all_cloud_inventory(
        session,
        registry,
        EncryptedSshCredentialStore(app_settings),
        checkpoint=session.commit,
    )
    session.commit()
    return result


@app.post("/api/v1/targets/alibaba-ecs/sync")
def sync_alibaba_targets(
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
    region: str = Query(default="cn-hangzhou", min_length=3, max_length=40),
    instance_id: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    records = sync_ecs_inventory(
        session,
        region,
        instance_ids=instance_id,
        credential_store=EncryptedSshCredentialStore(app_settings),
    )
    session.commit()
    return {"items": [target_view(item) for item in records], "total": len(records)}


@app.post("/api/v1/targets/import", status_code=201)
def import_target(
    payload: ImportExternalTargetRequest,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = import_external_target(session, payload)
    session.commit()
    return target_view(record)


@app.post("/api/v1/targets/connect", status_code=201)
def connect_target(
    payload: ConnectExternalTargetRequest,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = connect_external_target(session, payload)
    if payload.deploy_worker:
        deployment = deploy_remote_worker(payload, record, app_settings)
        host_key = str(record.fingerprint_json.get("host_key_sha256") or "")
        remembered = (
            EncryptedSshCredentialStore(app_settings).save(record.id, payload, host_key)
            if payload.remember_credentials
            else False
        )
        # Mark the target as available and runnable after successful Worker deployment
        record.status = "available"
        record.runnable = True
    session.commit()
    result = target_view(record)
    if payload.deploy_worker:
        result["deployment"] = deployment
        result["credentialsRemembered"] = remembered
    return result


@app.get("/api/v1/targets/{target_id}/worker")
def get_target_worker(target_id: str, session: SessionDependency) -> dict[str, Any]:
    record = session.get(TargetRecord, target_id)
    if record is None:
        raise HTTPException(status_code=404, detail="target not found")
    return deployment_status(target_id)


def _automatic_target_ssh_request(
    target: TargetRecord,
    app_settings: Settings,
    store: EncryptedSshCredentialStore,
) -> ConnectExternalTargetRequest:
    """Resolve the same saved/default SSH credentials used by the manual test."""

    endpoint = str((target.inventory_json or {}).get("endpoint") or "").strip()
    if not endpoint:
        raise ExternalTargetError("cloud target has no reachable endpoint")
    if target.id in store.verified_target_ids():
        return remembered_target_request(target, app_settings)
    try:
        return store.load_pending(target.id, endpoint)
    except RemoteCredentialError:
        if target.provider == "external":
            raise
    credentials = _default_cloud_ssh_credentials(
        app_settings,
        remember_credentials=True,
    )
    return ConnectExternalTargetRequest(
        endpoint=endpoint,
        port=credentials.port,
        username=credentials.username,
        auth_method=credentials.auth_method,
        password=credentials.password,
        private_key=credentials.private_key,
        passphrase=credentials.passphrase,
        deploy_worker=True,
        remember_credentials=True,
    )


def _verify_target_ssh_for_worker(
    session: Session,
    target: TargetRecord,
    app_settings: Settings,
) -> tuple[TargetRecord, ConnectExternalTargetRequest, bool]:
    """Probe SSH, pin the observed host key, and promote credentials for recovery."""

    store = EncryptedSshCredentialStore(app_settings)
    request = _automatic_target_ssh_request(target, app_settings, store)
    refreshed = (
        connect_external_target(session, request)
        if target.provider == "external"
        else connect_existing_target(session, target, request)
    )
    if refreshed.id != target.id:
        raise ExternalTargetError("SSH endpoint no longer resolves to the selected target")
    # Persist the verified inventory before the Worker registers through a
    # separate API request, then make the pinned credentials recovery-safe.
    session.commit()
    host_key = str(refreshed.fingerprint_json.get("host_key_sha256") or "")
    remembered = store.save(refreshed.id, request, host_key)
    return refreshed, request, remembered


@app.post("/api/v1/targets/{target_id}/ssh-test")
def test_target_ssh_connection(
    target_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    """Verify SSH with encrypted saved credentials and restore the remote Worker."""

    target = session.get(TargetRecord, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="target not found")
    refreshed, request, remembered = _verify_target_ssh_for_worker(
        session,
        target,
        app_settings,
    )
    deployment = deploy_remote_worker(request, refreshed, app_settings)
    if target.provider != "external":
        refreshed.status = "available"
        refreshed.runnable = True
        refreshed.inventory_json = {
            **(refreshed.inventory_json or {}),
            "autoSsh": {
                "status": "connected",
                "deployment": deployment.get("status", "deployed"),
            },
        }
        refreshed.snapshot_digest = canonical_digest(
            {"fingerprint": refreshed.fingerprint_json, "inventory": refreshed.inventory_json}
        )
        session.commit()
    result = target_view(refreshed)
    result["credentialsRemembered"] = remembered
    result["connectionTest"] = {
        "status": "connected",
        "testedAt": utc_now().isoformat(),
        "hostKeySha256": refreshed.fingerprint_json.get("host_key_sha256"),
    }
    result["deployment"] = deployment
    return result


@app.post("/api/v1/targets/{target_id}/ssh-connect", status_code=200)
def connect_existing_target_ssh(
    target_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    _operator: OperatorDependency,
    payload: ConnectExternalTargetRequest | None = None,
) -> dict[str, Any]:
    """Verify SSH and bind encrypted credentials to a purchased target."""

    target = session.get(TargetRecord, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="target not found")
    if target.provider == "external":
        raise HTTPException(status_code=400, detail="use the external target connect endpoint")
    if payload is None:
        endpoint = str((target.inventory_json or {}).get("endpoint") or "")
        if not endpoint:
            raise HTTPException(status_code=409, detail="cloud target has no reachable endpoint")
        credentials = _default_cloud_ssh_credentials(app_settings, remember_credentials=True)
        payload = ConnectExternalTargetRequest(
            endpoint=endpoint,
            port=credentials.port,
            username=credentials.username,
            auth_method=credentials.auth_method,
            password=credentials.password,
            private_key=credentials.private_key,
            passphrase=credentials.passphrase,
            deploy_worker=True,
            remember_credentials=True,
        )
    refreshed = connect_existing_target(session, target, payload)
    deployment = deploy_remote_worker(payload, refreshed, app_settings)
    host_key = str(refreshed.fingerprint_json.get("host_key_sha256") or "")
    remembered = (
        EncryptedSshCredentialStore(app_settings).save(refreshed.id, payload, host_key)
        if payload.remember_credentials
        else False
    )
    refreshed.status = "available"
    refreshed.runnable = True
    session.commit()
    result = target_view(refreshed)
    result["credentialsRemembered"] = remembered
    result["connectionTest"] = {
        "status": "connected",
        "testedAt": utc_now().isoformat(),
        "hostKeySha256": host_key,
    }
    result["deployment"] = deployment
    return result


@app.get("/api/v1/targets/{target_id}/destroy-preview")
def target_destroy_preview(
    target_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    operator: OperatorDependency,
) -> dict[str, Any]:
    if operator != "operator":
        raise CloudWorkflowError(
            "destroy preview requires operator authentication",
            status_code=401,
            code="operator_auth_required",
        )
    return destroy_target_preview(session, app_settings, target_id)


@app.post("/api/v1/targets/{target_id}/destroy")
def target_destroy(
    target_id: str,
    request: TargetDestroyRequest,
    session: SessionDependency,
    app_settings: SettingsDependency,
    registry: ProviderRegistryDependency,
    operator: OperatorDependency,
) -> dict[str, Any]:
    if operator != "operator":
        raise CloudWorkflowError(
            "destroy requires operator authentication",
            status_code=401,
            code="operator_auth_required",
        )
    result = destroy_target(session, app_settings, registry, target_id, request)
    session.commit()
    return result


@app.patch("/api/v1/targets/{target_id}/cloud-endpoint")
def update_cloud_endpoint(
    target_id: str,
    payload: dict[str, Any],
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    """Update cloud target endpoint information (public IP, etc.)."""

    target = session.get(TargetRecord, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="target not found")
    if target.provider not in {"alibaba", "tencent", "baidu", "volcengine"}:
        raise HTTPException(status_code=400, detail="not a cloud target")

    inventory = target.inventory_json or {}
    now = utc_now()

    # Update endpoint and public IP if provided
    if "public_ip" in payload:
        inventory["public_ip"] = payload["public_ip"]
        inventory["endpoint"] = payload["public_ip"]
        inventory["public_ip_present"] = bool(payload["public_ip"])

    if "private_ip" in payload:
        inventory["private_ip"] = payload["private_ip"]

    if "status" in payload:
        inventory["status"] = payload["status"]
        # Update target status based on instance state
        status_upper = str(payload["status"]).upper()
        if status_upper == "RUNNING":
            target.status = "inventory-only"
        elif status_upper in {"STOPPED", "TERMINATED"}:
            target.status = "offline"

    # Update fingerprint with hardware info if provided
    fingerprint = target.fingerprint_json or {}
    if "instance_type" in payload:
        fingerprint["instance_type"] = payload["instance_type"]
    if "cpu_core_count" in payload:
        fingerprint["logical_cpu_count"] = payload["cpu_core_count"]
    if "memory_gib" in payload:
        fingerprint["memory_gib"] = payload["memory_gib"]

    target.inventory_json = inventory
    target.fingerprint_json = fingerprint
    target.last_inventory_seen_at = now
    target.updated_at = now

    session.commit()
    return target_view(target)


@app.get("/api/v1/experiments")
def list_experiments(
    session: SessionDependency,
    status: str | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    statement = select(ExperimentRecord).order_by(ExperimentRecord.created_at.desc()).limit(limit)
    if status:
        statement = statement.where(ExperimentRecord.status == status)
    if search:
        statement = statement.where(ExperimentRecord.name.contains(search))
    records = list(session.scalars(statement))
    return {"items": [experiment_view(session, item) for item in records], "total": len(records)}


@app.post("/api/v1/experiments", status_code=201)
def create_experiment_endpoint(
    payload: dict[str, Any], session: SessionDependency
) -> dict[str, Any]:
    request = _normalize_create_request(payload, session)
    record = create_experiment(session, request)
    session.commit()
    return experiment_view(session, record, detail=True)


def _normalize_create_request(payload: dict[str, Any], session: Session) -> ExperimentCreate:
    if "spec" in payload:
        return ExperimentCreate.model_validate(payload)
    if payload.get("mode") == ExperimentMode.SELECTION:
        return _selection_create_request(payload, session)
    raise SchedulerError("only selection-mode experiments can be created through this endpoint")


def _selection_create_request(payload: dict[str, Any], session: Session) -> ExperimentCreate:
    benchmark_id = str(payload.get("benchmarkId") or "")
    if is_retired_benchmark(benchmark_id):
        raise SchedulerError("scenario benchmark version is not installed")
    benchmark_version = str(payload.get("benchmarkVersion") or "")
    current = session.scalar(
        select(BenchmarkRecord)
        .where(BenchmarkRecord.benchmark_id == benchmark_id)
        .order_by(BenchmarkRecord.installed_at.desc(), BenchmarkRecord.key.desc())
        .limit(1)
    )
    benchmark = current
    if benchmark_version:
        benchmark = session.scalar(
            select(BenchmarkRecord).where(
                BenchmarkRecord.benchmark_id == benchmark_id,
                BenchmarkRecord.version == benchmark_version,
            )
        )
    if benchmark is None:
        raise SchedulerError("scenario benchmark version is not installed")
    if current is None or benchmark.key != current.key:
        raise SchedulerError(
            "selected benchmark version has been replaced; use the current catalog version"
        )
    registration = session.scalar(
        select(BenchmarkRegistrationRecord).where(
            BenchmarkRegistrationRecord.benchmark_key == benchmark.key,
            BenchmarkRegistrationRecord.status == "registered",
        )
    )
    scenario_document = selection_scenario_document(benchmark, registration)
    if scenario_document is None:
        raise SchedulerError("selected benchmark is missing a selection scenario contract")
    if not benchmark_view(benchmark, registration)["selectionReady"]:
        raise SchedulerError(
            "selected benchmark is not directly testable; install a trusted executable package "
            "with automatic target provisioning"
        )
    require_single_node_contract(benchmark.manifest_json)
    scenario = ScenarioBenchmarkSpec.model_validate(scenario_document)

    raw_target_ids = payload.get("targetIds")
    if not isinstance(raw_target_ids, list) or not raw_target_ids:
        raise SchedulerError("selection study requires at least one target")
    target_ids = [str(target_id) for target_id in raw_target_ids]
    if len(target_ids) != 1:
        raise BenchmarkTargetCompatibilityError(
            "单机 Benchmark 必须且只能选择一台机器",
            [{
                "code": "single_target_required",
                "field": "targetIds",
                "required": 1,
                "actual": len(target_ids),
                "message": "单机 Benchmark 不允许提交多个机器 ID",
            }],
        )
    selected_target = session.get(TargetRecord, target_ids[0])
    if selected_target is None:
        raise BenchmarkTargetCompatibilityError(
            "所选资源不存在",
            [{
                "code": "target_not_found",
                "field": "targetIds[0]",
                "required": "已登记的活动资源",
                "actual": target_ids[0],
                "message": "资源可能已被删除或尚未同步",
            }],
        )
    assert_target_compatible(benchmark.manifest_json, selected_target)
    placement_pair_id = str(payload.get("placementPairId") or "placement-1")
    supplied_bindings = payload.get("targetBindings")
    binding_overrides = (
        {
            str(item.get("targetId")): item
            for item in supplied_bindings
            if isinstance(item, dict) and item.get("targetId")
        }
        if isinstance(supplied_bindings, list)
        else {}
    )
    target_bindings: list[TargetBindingSpec] = []
    for target_id in target_ids:
        target = session.get(TargetRecord, target_id)
        override = binding_overrides.get(target_id, {})
        inventory = target.inventory_json if target else {}
        variant_id = str(
            override.get("variantId")
            or inventory.get("instance_type")
            or inventory.get("instanceType")
            or target_id
        )
        target_bindings.append(
            TargetBindingSpec(
                target_id=target_id,
                variant_id=variant_id,
                label=str(override.get("label") or (target.name if target else target_id)),
                placement_pair_id=str(override.get("placementPairId") or placement_pair_id),
                price=PriceSnapshot.model_validate(override["price"])
                if isinstance(override.get("price"), dict)
                else None,
            )
        )

    metric_declaration = benchmark.manifest_json["spec"]["metrics"].get(scenario.primary_metric)
    if metric_declaration is None:
        raise SchedulerError("scenario primary metric is not declared")
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    defaults = benchmark_selection_defaults(benchmark.manifest_json)
    repeats = int(config.get("repeats", payload.get("repeats", defaults["repeats"])))
    seed = int(config.get("seed", payload.get("seed", defaults["seed"])))
    wall_time_seconds = int(
        config.get("timeout", payload.get("timeout", defaults["timeout"]))
    )
    raw_input_bindings = payload.get("inputBindings")
    input_bindings = (
        {
            str(input_id): BenchmarkInputBinding.model_validate(binding)
            for input_id, binding in raw_input_bindings.items()
        }
        if isinstance(raw_input_bindings, dict)
        else {}
    )
    workload_ids = [str(item["id"]) for item in benchmark.manifest_json["spec"]["workloads"]]
    if scenario.load_search is not None:
        load_point_budget = (
            len(scenario.load_search.common_load_fractions)
            + scenario.load_search.maximum_adaptive_points
        )
        attempt_budget = max(
            1,
            len(target_ids)
            * len(workload_ids)
            * scenario.load_search.boundary_repeats
            * load_point_budget
            * 2,
        )
    else:
        attempt_budget = max(1, len(target_ids) * len(workload_ids) * repeats)
    tail_min_samples = scenario.tail_evidence.minimum_samples if scenario.tail_evidence else 100
    spec = ExperimentSpec(
        mode=ExperimentMode.SELECTION,
        benchmark_id=benchmark.benchmark_id,
        benchmark_version=benchmark.version,
        target_ids=target_ids,
        workload_ids=workload_ids,
        input_bindings=input_bindings,
        objectives=[
            ObjectiveSpec(
                metric=scenario.primary_metric,
                unit=str(metric_declaration["unit"]),
                direction=Direction(metric_declaration["direction"]),
                aggregation=Aggregation.MEDIAN,
                comparison=Comparison.RELATIVE,
                minimum_samples=repeats,
            )
        ],
        design=ExperimentalDesign(
            warmup_runs=1,
            min_repeats=repeats,
            max_repeats=repeats,
            max_retries=1,
            baseline_every_n=1,
            confidence_level=0.95,
            bootstrap_resamples=2000,
            tail_min_samples=tail_min_samples,
            random_seed=seed,
        ),
        budget=BudgetSpec(
            max_candidates=1,
            max_attempts=attempt_budget,
            wall_time_seconds=wall_time_seconds,
        ),
        scenario=scenario,
        selection=SelectionDesign(
            target_bindings=target_bindings,
            reference_offered_load=(
                float(config["referenceOfferedLoad"])
                if config.get("referenceOfferedLoad") is not None
                else None
            ),
            order_scheme="balanced-random",
            inference_unit="time_block",
            minimum_placement_pairs=len({binding.placement_pair_id for binding in target_bindings}),
            random_seed=seed,
        ),
    )
    return ExperimentCreate(
        name=str(payload.get("name") or scenario.name),
        description=str(payload.get("description") or scenario.decision_question),
        spec=spec,
    )


@app.get("/api/v1/experiments/{experiment_id}")
def get_experiment(experiment_id: str, session: SessionDependency) -> dict[str, Any]:
    record = session.get(ExperimentRecord, experiment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment_view(session, record, detail=True)


@app.delete("/api/v1/experiments/{experiment_id}", status_code=204)
def delete_experiment(
    experiment_id: str,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> None:
    record = session.get(ExperimentRecord, experiment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    if record.status in {"queued", "running", "paused"}:
        raise HTTPException(
            status_code=409,
            detail="cancel an active experiment before deleting it",
        )
    session.delete(record)
    session.commit()


@app.get("/api/v1/experiments/{experiment_id}/analysis")
def get_analysis(experiment_id: str, session: SessionDependency) -> dict[str, Any]:
    if session.get(ExperimentRecord, experiment_id) is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    result = build_analysis_snapshot(session, experiment_id, persist=True)
    session.commit()
    return analysis_view(result)


@app.get("/api/v1/experiments/{experiment_id}/post-optimization")
def get_post_optimization(experiment_id: str, session: SessionDependency) -> dict[str, Any]:
    experiment = session.get(ExperimentRecord, experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return post_optimization_view(session, experiment)


@app.post("/api/v1/experiments/{experiment_id}/post-optimization", status_code=201)
def create_post_optimization(experiment_id: str, session: SessionDependency) -> dict[str, Any]:
    experiment = session.get(ExperimentRecord, experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    result = start_post_optimization(session, experiment)
    session.commit()
    return result


@app.get("/api/v1/experiments/{experiment_id}/variability")
def get_variability(experiment_id: str, session: SessionDependency) -> dict[str, Any]:
    if session.get(ExperimentRecord, experiment_id) is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    try:
        result = build_variability_report(session, experiment_id, persist=True)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    session.commit()
    return result


def _ensure_experiment_workers(
    session: Session,
    experiment: ExperimentRecord,
    app_settings: Settings,
) -> None:
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    target_ids = list(spec.target_ids)
    if spec.selection is not None and spec.selection.load_generator_target_id:
        target_ids = [spec.selection.load_generator_target_id]
    for target_id in dict.fromkeys(target_ids):
        target = session.get(TargetRecord, target_id)
        provider = target.provider if target is not None else None
        if provider not in REMOTE_TARGET_PROVIDERS:
            continue
        try:
            deployment = ensure_target_worker(target_id, app_settings)
        except RemoteWorkerRecoveryError:
            # A newly purchased cloud target can have encrypted pending/default
            # credentials without a manually pinned SSH identity. Perform the
            # same probe as the SSH button before allowing work into the queue.
            store = EncryptedSshCredentialStore(app_settings)
            if target is None or target_id in store.verified_target_ids():
                raise
            _verify_target_ssh_for_worker(session, target, app_settings)
            deployment = ensure_target_worker(target_id, app_settings)
        # Registration is committed by a separate Worker request. Discard
        # identity-map snapshots so scheduler readiness sees the refreshed
        # runnable/capability projection instead of the pre-recovery row.
        session.expire_all()
        refreshed = session.get(TargetRecord, target_id)
        if refreshed is not None and refreshed.provider != "external":
            deployment_status_value = deployment or {}
            refreshed.inventory_json = {
                **(refreshed.inventory_json or {}),
                "autoSsh": {
                    "status": "connected",
                    "deployment": deployment_status_value.get("status", "ready"),
                },
            }
            refreshed.snapshot_digest = canonical_digest(
                {
                    "fingerprint": refreshed.fingerprint_json,
                    "inventory": refreshed.inventory_json,
                }
            )


def _experiment_action(
    session: Session,
    experiment_id: str,
    action: str,
    app_settings: Settings | None = None,
) -> dict[str, Any]:
    record = session.get(ExperimentRecord, experiment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    actions = {
        "start": start_experiment,
        "pause": pause_experiment,
        "resume": resume_experiment,
        "cancel": cancel_experiment,
    }
    if action in {"start", "resume"}:
        _ensure_experiment_workers(session, record, app_settings or get_settings())
    updated = actions[action](session, record)
    session.commit()
    return experiment_view(session, updated, detail=True)


@app.post("/api/v1/experiments/{experiment_id}/start")
def start_action(
    experiment_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
) -> dict[str, Any]:
    return _experiment_action(session, experiment_id, "start", app_settings)


@app.post("/api/v1/experiments/{experiment_id}/pause")
def pause_action(experiment_id: str, session: SessionDependency) -> dict[str, Any]:
    return _experiment_action(session, experiment_id, "pause")


@app.post("/api/v1/experiments/{experiment_id}/resume")
def resume_action(
    experiment_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
) -> dict[str, Any]:
    return _experiment_action(session, experiment_id, "resume", app_settings)


@app.post("/api/v1/experiments/{experiment_id}/cancel")
def cancel_action(experiment_id: str, session: SessionDependency) -> dict[str, Any]:
    return _experiment_action(session, experiment_id, "cancel")


@app.post("/api/v1/attempts/{attempt_id}/retry", status_code=201)
def retry_attempt_endpoint(attempt_id: str, session: SessionDependency) -> dict[str, Any]:
    attempt = session.get(AttemptRecord, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="attempt not found")
    created = retry_attempt(session, attempt)
    session.commit()
    return {
        "id": created.id,
        "status": created.status,
        "repeatIndex": created.repeat_index,
        "retryIndex": created.retry_index,
    }


@app.get("/api/v1/experiments/{experiment_id}/events")
async def experiment_events(
    experiment_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        sequence = after
        while not await request.is_disconnected():
            with SessionLocal() as session:
                events = list(
                    session.scalars(
                        select(EventRecord)
                        .where(
                            EventRecord.experiment_id == experiment_id,
                            EventRecord.sequence > sequence,
                        )
                        .order_by(EventRecord.sequence)
                    )
                )
            for event in events:
                sequence = event.sequence
                data = {
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "entityType": event.entity_type,
                    "entityId": event.entity_id,
                    "payload": event.payload_json,
                    "createdAt": event.created_at.isoformat(),
                }
                yield (
                    f"id: {event.sequence}\nevent: {event.event_type}\ndata: {json.dumps(data)}\n\n"
                )
            if not events:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/v1/artifacts/{digest}")
def download_artifact(digest: str, session: SessionDependency) -> FileResponse:
    normalized = digest if digest.startswith("sha256:") else f"sha256:{digest}"
    artifact = session.get(ArtifactRecord, normalized)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    link = session.scalar(
        select(ArtifactLinkRecord).where(ArtifactLinkRecord.digest == normalized).limit(1)
    )
    cas = FileSystemCAS(settings.artifact_dir, settings.max_artifact_bytes)
    stored = cas.verify(normalized, expected_size=artifact.size)
    return FileResponse(
        stored.path,
        media_type=link.media_type if link else "application/octet-stream",
        filename=link.name if link else normalized.removeprefix("sha256:"),
    )


@app.get("/api/v1/experiments/{experiment_id}/evidence")
def export_evidence(
    experiment_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
) -> FileResponse:
    export_dir = app_settings.data_dir / "exports"
    destination = export_dir / f"{experiment_id}.looper-evidence.zip"
    cas = FileSystemCAS(app_settings.artifact_dir, app_settings.max_artifact_bytes)
    build_evidence_bundle(session, experiment_id, cas, destination)
    return FileResponse(
        destination,
        media_type="application/zip",
        filename=destination.name,
    )


@app.post("/api/v1/evidence/verify")
def verify_evidence(
    app_settings: SettingsDependency,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    verify_dir = app_settings.data_dir / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    destination = verify_dir / "uploaded-evidence.zip"
    with destination.open("wb") as stream:
        while chunk := file.file.read(1024 * 1024):
            stream.write(chunk)
    try:
        return verify_evidence_bundle(destination)
    finally:
        destination.unlink(missing_ok=True)


@app.post("/api/v1/workers/register")
def register_worker_endpoint(
    request: WorkerRegister, session: SessionDependency, app_settings: SettingsDependency
) -> dict[str, Any]:
    worker = register_worker(session, app_settings, request)
    session.commit()
    return {
        "workerId": worker.id,
        "status": worker.status,
        "leaseSeconds": app_settings.lease_seconds,
    }


@app.post("/api/v1/workers/claim")
def claim_worker_endpoint(
    request: WorkerClaim,
    session: SessionDependency,
    app_settings: SettingsDependency,
    token: WorkerToken,
) -> dict[str, Any]:
    worker = authenticate_worker(session, app_settings, request.worker_id, token)
    claim = claim_attempt(session, app_settings, worker)
    session.commit()
    return {"claim": claim}


@app.post("/api/v1/worker-attempts/{attempt_id}/heartbeat")
def heartbeat_worker_endpoint(
    attempt_id: str,
    request: AttemptHeartbeat,
    session: SessionDependency,
    app_settings: SettingsDependency,
    token: WorkerToken,
) -> dict[str, Any]:
    authenticate_worker(session, app_settings, request.worker_id, token)
    result = heartbeat_attempt(session, app_settings, attempt_id, request)
    session.commit()
    return result


@app.post("/api/v1/worker-attempts/{attempt_id}/start")
def start_worker_attempt_endpoint(
    attempt_id: str,
    request: AttemptStart,
    session: SessionDependency,
    app_settings: SettingsDependency,
    token: WorkerToken,
) -> dict[str, Any]:
    authenticate_worker(session, app_settings, request.worker_id, token)
    attempt = start_attempt(session, attempt_id, request)
    session.commit()
    return {"id": attempt.id, "status": attempt.status, "envelopeDigest": attempt.envelope_digest}


@app.post("/api/v1/worker-attempts/{attempt_id}/artifacts", status_code=201)
def upload_worker_artifact_endpoint(
    attempt_id: str,
    session: SessionDependency,
    app_settings: SettingsDependency,
    token: WorkerToken,
    file: UploadFile = File(...),
    worker_id: str = Form(alias="workerId"),
    fencing_token: int = Form(alias="fencingToken"),
    role: str = Form(...),
    name: str = Form(...),
    media_type: str = Form(alias="mediaType"),
    producer: str = Form(default="benchmark"),
) -> dict[str, Any]:
    authenticate_worker(session, app_settings, worker_id, token)
    metadata = ArtifactMetadata(
        workerId=worker_id,
        fencingToken=fencing_token,
        role=role,
        name=name,
        mediaType=media_type,
        producer=producer,
    )
    cas = FileSystemCAS(app_settings.artifact_dir, app_settings.max_artifact_bytes)
    link, stored = store_artifact(session, cas, attempt_id, metadata, file.file)
    session.commit()
    return {"id": link.id, "digest": stored.digest, "size": stored.size}


@app.post("/api/v1/worker-attempts/{attempt_id}/complete")
def complete_worker_attempt_endpoint(
    attempt_id: str,
    request: AttemptCompletion,
    session: SessionDependency,
    app_settings: SettingsDependency,
    token: WorkerToken,
) -> dict[str, Any]:
    authenticate_worker(session, app_settings, request.worker_id, token)
    attempt = complete_attempt(session, attempt_id, request)
    session.commit()
    return {"id": attempt.id, "status": attempt.status, "completedAt": attempt.completed_at}
