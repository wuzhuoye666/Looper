from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


class AdapterError(ValueError):
    pass


DIRECTION_MAP = {
    "higher-is-better": "maximize",
    "lower-is-better": "minimize",
    "none": "none",
}


def load_adapter_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    value = (
        yaml.safe_load(text)
        if path.suffix.lower() in {".yaml", ".yml"}
        else json.loads(text)
    )
    if not isinstance(value, dict):
        raise AdapterError("adapter input must be an object")
    return value


def json_path(document: Any, path: str) -> Any:
    if not path.startswith("$."):
        raise AdapterError(f"unsupported path syntax: {path}")
    value = document
    for component in path[2:].split("."):
        if not component or not isinstance(value, dict) or component not in value:
            raise AdapterError(f"required path is missing: {path}")
        value = value[component]
    return value


def apply_adapter(manifest: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("manifest_version") != 1:
        raise AdapterError("unsupported adapter manifest version")
    result = {
        "adapter_id": manifest["adapter_id"],
        "upstream_id": manifest["upstream_id"],
        "synthetic": bool(manifest.get("synthetic", False)),
        "fields": {},
        "parameters": {},
        "metrics": [],
    }
    for target, path in manifest.get("result_mapping", {}).items():
        result["fields"][target] = json_path(document, path)
    for mapping in manifest.get("parameter_mappings", []):
        result["parameters"][mapping["target"]] = {
            "value": json_path(document, mapping["source"]),
            "unit": mapping.get("unit"),
        }
    for mapping in manifest.get("metric_mappings", []):
        value = json_path(document, mapping["source"])
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AdapterError(f"metric {mapping['target']} must be numeric")
        direction = DIRECTION_MAP.get(mapping["direction"])
        if direction is None:
            raise AdapterError(f"unknown metric direction: {mapping['direction']}")
        result["metrics"].append(
            {
                "metric": mapping["target"],
                "value": float(value),
                "unit": mapping["unit"],
                "direction": direction,
            }
        )
    metric_catalog_path = manifest.get("metric_catalog")
    if metric_catalog_path:
        catalog = json_path(document, metric_catalog_path)
        if not isinstance(catalog, list):
            raise AdapterError("metric catalog must be an array")
        result["metric_catalog"] = [
            {
                **item,
                "direction": DIRECTION_MAP.get(item.get("direction"), item.get("direction")),
            }
            for item in catalog
            if isinstance(item, dict)
        ]
    return result


def load_and_apply_adapter(manifest_path: Path, input_path: Path) -> dict[str, Any]:
    manifest = load_adapter_document(manifest_path)
    document = load_adapter_document(input_path)
    return apply_adapter(manifest, document)
