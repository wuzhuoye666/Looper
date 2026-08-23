"""Shared interpretation of Benchmark deployment requirements."""

from __future__ import annotations

from typing import Any


def provisioning_contract(manifest: dict[str, Any]) -> dict[str, Any] | None:
    value = manifest["spec"]["runtime"].get("provisioning")
    return value if isinstance(value, dict) and value.get("mode") == "managed" else None


def deployment_capabilities(manifest: dict[str, Any]) -> set[str]:
    """Capabilities that must exist before Looper can deploy the package.

    Benchmark-specific software listed in ``spec.capabilities`` may be supplied
    by a managed ``prepare`` phase. Legacy packages keep the old preinstalled
    capability behavior.
    """

    provisioning = provisioning_contract(manifest)
    if provisioning is None:
        return set(manifest["spec"].get("capabilities", []))
    return {str(item) for item in provisioning.get("hostCapabilities", [])}


def provisioned_capabilities(manifest: dict[str, Any]) -> set[str]:
    provisioning = provisioning_contract(manifest)
    if provisioning is None:
        return set()
    return {str(item) for item in provisioning.get("provides", [])}
