from __future__ import annotations

import secrets
import shutil
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import dotenv_values, set_key

_PROVIDER_CREDENTIALS = {
    "tencent": (
        ("TENCENTCLOUD_SECRET_ID", "SecretId", True),
        ("TENCENTCLOUD_SECRET_KEY", "SecretKey", True),
        ("TENCENTCLOUD_SESSION_TOKEN", "SessionToken", False),
    ),
    "alibaba": (
        ("ALIBABA_CLOUD_ACCESS_KEY_ID", "AccessKey ID", True),
        ("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "AccessKey Secret", True),
        ("ALIBABA_CLOUD_SECURITY_TOKEN", "SecurityToken", False),
    ),
}


@dataclass(frozen=True)
class CloudSetupResult:
    provider: str
    env_file: Path
    operator_token: str
    max_hourly_amount: Decimal


def credential_fields(provider: str) -> tuple[tuple[str, str, bool], ...]:
    normalized = provider.strip().lower()
    try:
        return _PROVIDER_CREDENTIALS[normalized]
    except KeyError as error:
        raise ValueError("provider must be tencent or alibaba") from error


def _validated_secret(name: str, value: str, *, required: bool) -> str:
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{name} is required")
    if any(character in normalized for character in ("\r", "\n", "\x00")):
        raise ValueError(f"{name} contains forbidden control characters")
    if len(normalized) > 4096:
        raise ValueError(f"{name} is too long")
    return normalized


def _strong_existing(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized if len(normalized) >= 32 else None


def configure_cloud_purchase(
    provider: str,
    credentials: dict[str, str],
    *,
    env_file: Path = Path(".env"),
    template_file: Path = Path(".env.example"),
    max_hourly_amount: Decimal = Decimal("10"),
) -> CloudSetupResult:
    normalized_provider = provider.strip().lower()
    fields = credential_fields(normalized_provider)
    if max_hourly_amount <= 0:
        raise ValueError("max hourly amount must be greater than zero")

    env_file = env_file.resolve()
    if not env_file.exists():
        env_file.parent.mkdir(parents=True, exist_ok=True)
        if template_file.exists():
            shutil.copyfile(template_file, env_file)
        else:
            env_file.touch()

    existing = dotenv_values(env_file)
    operator_token = _strong_existing(existing.get("LOOPER_OPERATOR_TOKEN"))
    if operator_token is None:
        operator_token = secrets.token_urlsafe(32)
    confirmation_secret = _strong_existing(existing.get("LOOPER_PURCHASE_CONFIRMATION_SECRET"))
    if confirmation_secret is None or confirmation_secret == operator_token:
        confirmation_secret = secrets.token_urlsafe(48)

    allowlist = {
        value.strip().lower()
        for value in str(existing.get("LOOPER_LIVE_PURCHASE_PROVIDERS") or "").split(",")
        if value.strip()
    }
    allowlist.add(normalized_provider)
    updates = {
        "LOOPER_LIVE_PURCHASE_ENABLED": "true",
        "LOOPER_LIVE_PURCHASE_PROVIDERS": ",".join(sorted(allowlist)),
        "LOOPER_OPERATOR_TOKEN": operator_token,
        "LOOPER_PURCHASE_CONFIRMATION_SECRET": confirmation_secret,
        "LOOPER_MAX_LIVE_HOURLY_AMOUNT": format(max_hourly_amount, "f"),
    }
    for variable, _label, required in fields:
        updates[variable] = _validated_secret(
            variable, credentials.get(variable, ""), required=required
        )

    for name, value in updates.items():
        set_key(str(env_file), name, value, quote_mode="always")

    return CloudSetupResult(
        provider=normalized_provider,
        env_file=env_file,
        operator_token=operator_token,
        max_hourly_amount=max_hourly_amount,
    )
