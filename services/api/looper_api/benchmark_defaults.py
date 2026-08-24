from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

_DEFAULT_REPEATS = 5
_DEFAULT_TIMEOUT_SECONDS = 86_400
_DEFAULT_SEED = 20_260_301


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def benchmark_selection_defaults(manifest: Mapping[str, Any]) -> dict[str, int]:
    """Return the benchmark-owned defaults used by the selection workflow."""
    metadata = manifest.get("metadata") or {}
    spec = manifest.get("spec") or {}
    extensions = spec.get("x-extensions") or {}
    declared = extensions.get("selectionDefaults") or {}
    audit = spec.get("audit") or {}

    repeats = _positive_int(
        declared.get("repeats"),
        _positive_int(audit.get("minimumRepeats"), _DEFAULT_REPEATS),
    )
    repeats = max(3, min(50, repeats))
    timeout = _positive_int(
        declared.get("timeoutSeconds", declared.get("timeout")),
        _DEFAULT_TIMEOUT_SECONDS,
    )
    timeout = max(300, min(31_536_000, timeout))

    identity = f"{metadata.get('id', 'benchmark')}@{metadata.get('version', '0')}"
    digest_seed = int.from_bytes(sha256(identity.encode("utf-8")).digest()[:4], "big")
    seed = _positive_int(declared.get("seed"), digest_seed % 2_000_000_000 or _DEFAULT_SEED)
    return {"repeats": repeats, "timeout": timeout, "seed": seed}


__all__ = ["benchmark_selection_defaults"]
