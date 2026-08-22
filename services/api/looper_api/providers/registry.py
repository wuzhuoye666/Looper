from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from looper_api.cloud_contracts import ProviderId
from looper_api.providers.base import CloudProvider, CloudProviderError

ProviderFactory = Callable[[], CloudProvider]


class CloudProviderRegistry:
    def __init__(self, factories: dict[ProviderId, ProviderFactory] | None = None) -> None:
        self._factories = factories or _default_factories()
        self._instances: dict[ProviderId, CloudProvider] = {}

    def get(self, provider_id: ProviderId | str) -> CloudProvider:
        try:
            normalized = ProviderId(provider_id)
        except ValueError as error:
            raise CloudProviderError(
                f"unsupported cloud provider: {provider_id}", code="unsupported_provider"
            ) from error
        factory = self._factories.get(normalized)
        if factory is None:
            raise CloudProviderError(
                f"cloud provider is not registered: {normalized}", code="unsupported_provider"
            )
        if normalized not in self._instances:
            self._instances[normalized] = factory()
        return self._instances[normalized]

    def all(self) -> list[CloudProvider]:
        return [self.get(provider_id) for provider_id in ProviderId]


def _default_factories() -> dict[ProviderId, ProviderFactory]:
    from looper_api.providers.alibaba_ecs import AlibabaEcsProvider
    from looper_api.providers.baidu_bcc import BaiduBccProvider
    from looper_api.providers.tencent_cvm import TencentCvmProvider
    from looper_api.providers.volcengine_ecs import VolcengineEcsProvider

    return {
        ProviderId.TENCENT: TencentCvmProvider,
        ProviderId.ALIBABA: AlibabaEcsProvider,
        ProviderId.VOLCENGINE: VolcengineEcsProvider,
        ProviderId.BAIDU: BaiduBccProvider,
    }


@lru_cache(maxsize=1)
def get_provider_registry() -> CloudProviderRegistry:
    return CloudProviderRegistry()
