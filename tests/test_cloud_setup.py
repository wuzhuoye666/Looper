from __future__ import annotations

from decimal import Decimal

import pytest
from dotenv import dotenv_values
from looper_api.cli import app
from looper_api.cloud_adoption import adopt_cloud_target
from looper_api.cloud_setup import configure_cloud_purchase
from looper_api.providers.utils import environment_credentials, optional_environment
from looper_api.serialization import target_view
from typer.testing import CliRunner


def test_explicit_env_file_is_used_without_sdk_default_chain(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "cloud.env"
    env_file.write_text(
        "TENCENTCLOUD_SECRET_ID=file-secret-id\n"
        "TENCENTCLOUD_SECRET_KEY=file-secret-key\n"
        "TENCENTCLOUD_SESSION_TOKEN=file-session-token\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOOPER_ENV_FILE", str(env_file))
    monkeypatch.delenv("TENCENTCLOUD_SECRET_ID", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_KEY", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SESSION_TOKEN", raising=False)

    values, missing = environment_credentials(
        ["TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"]
    )
    assert missing == []
    assert values == {
        "TENCENTCLOUD_SECRET_ID": "file-secret-id",
        "TENCENTCLOUD_SECRET_KEY": "file-secret-key",
    }
    assert optional_environment("TENCENTCLOUD_SESSION_TOKEN") == "file-session-token"


def test_external_cloud_adoption_uses_canonical_target_identity(db_session) -> None:
    record = adopt_cloud_target(
        db_session,
        provider="tencent",
        region="ap-guangzhou",
        zone="ap-guangzhou-6",
        instance_id="ins-live-1",
        name="looper-live",
        instance_type="SA9.MEDIUM2",
        image_id="img-ubuntu",
        state="RUNNING",
        cpu=2,
        memory_gib=2,
        private_ip="172.16.0.10",
        vpc_id="vpc-test",
        subnet_id="subnet-test",
    )
    assert record.id == "cloud:tencent:ap-guangzhou:ins-live-1"
    assert record.status == "inventory-only"
    assert record.inventory_json["source"] == "external-adoption"
    assert record.inventory_json["public_ip_present"] is False
    assert record.fingerprint_json["instance_type"] == "SA9.MEDIUM2"
    assert target_view(record)["status"] == "inventory"
    assert target_view(record)["endpoint"] == "172.16.0.10"
    assert target_view(record)["framework"] == "镜像 img-ubuntu"
    assert target_view(record)["version"] is None
    record.inventory_json = {**record.inventory_json, "instance_state": "TERMINATED"}
    assert target_view(record)["status"] == "offline"


def test_target_view_uses_legacy_cloud_inventory_hardware_fields(db_session) -> None:
    record = adopt_cloud_target(
        db_session,
        provider="alibaba",
        region="cn-hangzhou",
        zone="cn-hangzhou-h",
        instance_id="i-legacy",
        name="legacy-ecs",
        instance_type="ecs.c9i.2xlarge",
        image_id="img-linux",
        state="RUNNING",
        cpu=8,
        memory_gib=16,
    )
    # Older cloud syncs stored CPU and memory outside the fingerprint.
    record.fingerprint_json = {
        "instance_type": "ecs.c9i.2xlarge",
        "memory_gib": 16,
        "image_id": "ubuntu_24_04_x64_20G_alibase.vhd",
    }
    record.inventory_json = {
        **record.inventory_json,
        "cpu": 8,
        "memory_gib": 16,
    }

    view = target_view(record)
    assert view["hardware"] == "ecs.c9i.2xlarge · x86_64 · 8 vCPU · 16 GiB"
    assert view["framework"] == "镜像 ubuntu_24_04_x64_20G_alibase.vhd"
    assert view["version"] is None


def test_cloud_setup_writes_secrets_without_replacing_existing_configuration(tmp_path) -> None:
    template = tmp_path / ".env.example"
    env_file = tmp_path / ".env"
    template.write_text("LOOPER_DATA_DIR=.looper\n# Cloud configuration\n", encoding="utf-8")

    first = configure_cloud_purchase(
        "tencent",
        {
            "TENCENTCLOUD_SECRET_ID": "test-secret-id",
            "TENCENTCLOUD_SECRET_KEY": "test-secret-key",
            "TENCENTCLOUD_SESSION_TOKEN": "",
        },
        env_file=env_file,
        template_file=template,
        max_hourly_amount=Decimal("1.25"),
    )
    values = dotenv_values(env_file)
    assert values["LOOPER_DATA_DIR"] == ".looper"
    assert values["LOOPER_LIVE_PURCHASE_ENABLED"] == "true"
    assert values["LOOPER_LIVE_PURCHASE_PROVIDERS"] == "tencent"
    assert values["LOOPER_MAX_LIVE_HOURLY_AMOUNT"] == "1.25"
    assert values["TENCENTCLOUD_SECRET_ID"] == "test-secret-id"
    assert values["TENCENTCLOUD_SECRET_KEY"] == "test-secret-key"
    assert len(first.operator_token) >= 32
    assert values["LOOPER_OPERATOR_TOKEN"] == first.operator_token
    assert values["LOOPER_PURCHASE_CONFIRMATION_SECRET"] != first.operator_token

    second = configure_cloud_purchase(
        "alibaba",
        {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "test-access-key",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "test-access-secret",
            "ALIBABA_CLOUD_SECURITY_TOKEN": "",
        },
        env_file=env_file,
        template_file=template,
    )
    updated = dotenv_values(env_file)
    assert second.operator_token == first.operator_token
    assert updated["LOOPER_LIVE_PURCHASE_PROVIDERS"] == "alibaba,tencent"
    assert updated["TENCENTCLOUD_SECRET_KEY"] == "test-secret-key"


def test_cloud_setup_rejects_blank_or_multiline_required_credentials(tmp_path) -> None:
    with pytest.raises(ValueError, match="required"):
        configure_cloud_purchase(
            "tencent",
            {"TENCENTCLOUD_SECRET_ID": "", "TENCENTCLOUD_SECRET_KEY": "key"},
            env_file=tmp_path / ".env",
            template_file=tmp_path / "missing",
        )
    with pytest.raises(ValueError, match="control"):
        configure_cloud_purchase(
            "tencent",
            {
                "TENCENTCLOUD_SECRET_ID": "id\nother",
                "TENCENTCLOUD_SECRET_KEY": "key",
            },
            env_file=tmp_path / ".env",
            template_file=tmp_path / "missing",
        )


def test_cloud_configure_cli_hides_cloud_credentials(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("LOOPER_HOST=127.0.0.1\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "cloud",
            "configure",
            "tencent",
            "--env-file",
            str(tmp_path / ".env"),
            "--max-hourly-amount",
            "2.5",
        ],
        input="secret-id-value\nsecret-key-value\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "secret-id-value" not in result.output
    assert "secret-key-value" not in result.output
    assert "Provider allowlisted: tencent" in result.output
    assert "Hourly spend cap: 2.5" in result.output
    values = dotenv_values(tmp_path / ".env")
    assert values["TENCENTCLOUD_SECRET_ID"] == "secret-id-value"
    assert values["TENCENTCLOUD_SECRET_KEY"] == "secret-key-value"
