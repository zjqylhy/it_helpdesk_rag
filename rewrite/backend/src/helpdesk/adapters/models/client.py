import ssl

import httpx

from helpdesk.settings import Settings


def model_client(settings: Settings) -> httpx.AsyncClient:
    # 私有网关 CA 只增加可信根，保留证书和主机名校验；无自动直连或不验证回退。
    context = ssl.create_default_context()
    if settings.model_ca_file is not None:
        context.load_verify_locations(cafile=str(settings.model_ca_file))
    return httpx.AsyncClient(timeout=25, verify=context, trust_env=False, follow_redirects=False)
