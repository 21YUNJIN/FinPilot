"""商户客户端适配器包（C 侧）。

``client`` 提供与 ``kernel.provider_adapter.ProviderAdapter`` 兼容的本地与 HTTP 实现；
本包不做任何资金动作，只负责报价、预订、查询与撤单的对外契约。
"""

from .client import (
    HTTP_PROVIDER_ID,
    LOCAL_PROVIDER_ID,
    HttpMerchantAdapter,
    LocalMerchantAdapter,
    build_http_adapter,
    build_local_adapter,
    register_http,
    register_local,
)

__all__ = [
    "HTTP_PROVIDER_ID",
    "LOCAL_PROVIDER_ID",
    "HttpMerchantAdapter",
    "LocalMerchantAdapter",
    "build_http_adapter",
    "build_local_adapter",
    "register_http",
    "register_local",
]