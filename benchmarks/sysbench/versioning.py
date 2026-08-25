"""Shared Sysbench version parsing for provisioning and execution."""

from __future__ import annotations

import re

EXPECTED_VERSION = (1, 0, 20)
EXPECTED_VERSION_TEXT = ".".join(str(part) for part in EXPECTED_VERSION)
PREPARED_SCHEMA = "looper.sysbench.prepare/v2"
_VERSION_PATTERN = re.compile(
    r"^sysbench\s+(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?P<suffix>[-+][^\s]+)?(?:\s|$)",
    re.IGNORECASE,
)


def parse_sysbench_version(output: str) -> tuple[int, int, int] | None:
    """Return the semantic base version from ``sysbench --version`` output."""

    match = _VERSION_PATTERN.match(output.strip())
    if match is None:
        return None
    return tuple(int(match.group(name)) for name in ("major", "minor", "patch"))


def require_expected_version(output: str) -> str:
    """Validate the exact pinned base version and return normalized output."""

    normalized = output.strip()
    actual = parse_sysbench_version(normalized)
    if actual != EXPECTED_VERSION:
        received = normalized or "no version"
        raise ValueError(
            f"expected sysbench {EXPECTED_VERSION_TEXT}, received: {received}"
        )
    return normalized
