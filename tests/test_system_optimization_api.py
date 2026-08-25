from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from looper_api.config import Settings, get_settings
from looper_api.database import get_session
from looper_api.system_optimization_api import (
    SystemOptimizationActivateRequest,
    SystemOptimizationApproveRequest,
    SystemOptimizationCreateRequest,
    router,
)
from pydantic import ValidationError


def _digest(seed: str) -> str:
    return "sha256:" + seed * 64


def test_router_exposes_only_the_constrained_v1_lifecycle() -> None:
    routes = {
        (method, route.path)
        for route in router.routes
        for method in (route.methods or set())
    }

    assert routes == {
        ("GET", "/api/v1/system-optimization-baseline-context"),
        ("GET", "/api/v1/system-optimization-manifest"),
        ("GET", "/api/v1/system-optimization-runtime-profiles/{experiment_id}"),
        ("POST", "/api/v1/system-optimization-authorization-profiles"),
        ("POST", "/api/v1/system-optimization-studies"),
        ("GET", "/api/v1/system-optimization-studies/{study_id}"),
        ("POST", "/api/v1/system-optimization-studies/{study_id}/approve"),
        ("POST", "/api/v1/system-optimization-studies/{study_id}/activate"),
        ("POST", "/api/v1/system-optimization-studies/{study_id}/rollback"),
    }


def test_main_api_installs_the_constrained_router(db_session, tmp_path) -> None:
    from looper_api.app import app

    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        operator_token="",
        live_purchase_enabled=False,
    )
    try:
        response = TestClient(app).get(
            "/api/v1/system-optimization-studies/missing-study"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"] == "system optimization study not found"


def test_request_contracts_require_digest_bound_approval_and_activation() -> None:
    created = SystemOptimizationCreateRequest.model_validate(
        {
            "baselineCapacityStudyId": "capacity-a",
            "targetId": "target-a",
            "network": "internal",
            "minimumEffect": 0.05,
            "authorizationProfileDigest": _digest("a"),
            "runtimeProfileDigest": _digest("d"),
        }
    )
    approved = SystemOptimizationApproveRequest.model_validate(
        {"hypothesisDigest": _digest("b"), "expectedRevision": 3}
    )
    activated = SystemOptimizationActivateRequest.model_validate(
        {
            "decisionDigest": _digest("c"),
            "expectedRevision": 9,
            "authorizationProfileDigest": _digest("a"),
        }
    )

    assert created.minimum_effect == 0.05
    assert approved.expected_revision == 3
    assert activated.authorization_profile_digest == created.authorization_profile_digest
    with pytest.raises(ValidationError):
        SystemOptimizationActivateRequest.model_validate(
            {"decisionDigest": _digest("c"), "expectedRevision": 9}
        )


def test_missing_study_returns_without_external_side_effects(db_session, tmp_path) -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        operator_token="",
        live_purchase_enabled=False,
    )

    response = TestClient(app).get(
        "/api/v1/system-optimization-studies/missing-study"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "system optimization study not found"
