from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from looper_core.canonical import canonical_digest


class ManifestError(ValueError):
    pass


def find_repository_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "schemas" / "benchmark-manifest.schema.json").exists():
            return candidate
    raise ManifestError("could not locate repository schemas directory")


def load_document(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    value = (
        yaml.safe_load(raw)
        if path.suffix.lower() in {".yaml", ".yml"}
        else json.loads(raw)
    )
    if not isinstance(value, dict):
        raise ManifestError("manifest root must be an object")
    return value


def load_schema(name: str, repository_root: Path | None = None) -> dict[str, Any]:
    root = repository_root or find_repository_root()
    return json.loads((root / "schemas" / name).read_text(encoding="utf-8"))


def validate_document(document: dict[str, Any], schema_name: str) -> None:
    validator = Draft202012Validator(load_schema(schema_name))
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        rendered = "; ".join(
            f"{'/'.join(str(item) for item in error.path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise ManifestError(rendered)
    if schema_name == "benchmark-manifest.schema.json":
        _validate_diagnostic_recommendations(document)


def _validate_diagnostic_recommendations(document: dict[str, Any]) -> None:
    spec = document.get("spec") or {}
    contract = (spec.get("x-extensions") or {}).get("diagnosticRecommendations")
    if not isinstance(contract, dict):
        return
    policy = contract.get("policy") or {}
    if policy.get("cvStable", 0) >= policy.get("cvUnstable", 0):
        raise ManifestError(
            "spec/x-extensions/diagnosticRecommendations/policy: "
            "cvStable must be lower than cvUnstable"
        )
    audit_minimum = (spec.get("audit") or {}).get("minimumRepeats")
    if audit_minimum is not None and policy.get("minimumSamples") != audit_minimum:
        raise ManifestError(
            "spec/x-extensions/diagnosticRecommendations/policy: "
            "minimumSamples must equal audit.minimumRepeats"
        )
    workload_ids = {str(item.get("id")) for item in spec.get("workloads", [])}
    rules = contract.get("rules") or []
    rule_ids = [str(item.get("id")) for item in rules]
    if len(rule_ids) != len(set(rule_ids)):
        raise ManifestError(
            "spec/x-extensions/diagnosticRecommendations/rules: rule ids must be unique"
        )
    unknown = sorted(
        {
            str(workload_id)
            for rule in rules
            for workload_id in rule.get("workloadIds", [])
            if str(workload_id) not in workload_ids
        }
    )
    if unknown:
        raise ManifestError(
            "spec/x-extensions/diagnosticRecommendations/rules: "
            f"unknown workload ids: {unknown}"
        )


def load_and_validate_manifest(path: Path) -> tuple[dict[str, Any], str]:
    document = load_document(path)
    validate_document(document, "benchmark-manifest.schema.json")
    return document, canonical_digest(document)
