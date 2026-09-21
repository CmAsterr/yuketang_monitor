"""Model list discovery for OpenAI-compatible image providers.

Reference behavior: CC Switch model_fetch.rs at 06082e189d65e6d6dbadc35dacdac1ce6c79d89a.
Independent Python implementation: version-aware endpoints, 404/405 fallback,
data[].id / models[].slug formats. No provider errors or secrets returned verbatim.
"""

import asyncio
import re
import httpx
from .protocol import ProtocolError


def api_base(url):
    value = url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/models"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def model_urls(base):
    base = api_base(base)
    urls = (
        [base + "/models"]
        if re.search(r"/v\d+$", base)
        else [base + "/v1/models", base + "/models"]
    )
    if re.search(r"/v(?!1$)\d+$", base):
        urls.append(base + "/v1/models")
    return list(dict.fromkeys(urls))


async def fetch_models(base_url, api_key, *, transport=None):
    if not api_key:
        raise ProtocolError("请填写 API Key 或使用已保存的 Key")
    try:
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, transport=transport
            ) as client:
                for url in model_urls(base_url):
                    response = await client.get(
                        url, headers={"Authorization": "Bearer " + api_key}
                    )
                    if response.status_code in (404, 405):
                        continue
                    if response.status_code >= 300:
                        raise ProtocolError(
                            f"获取模型失败 HTTP {response.status_code}，请检查地址和 Key"
                        )
                    if len(response.content) > 2_000_000:
                        raise ProtocolError("模型列表响应过大")
                    try:
                        data = response.json()
                    except ValueError:
                        raise ProtocolError("模型列表不是有效 JSON") from None
                    entries = (
                        data.get("data", data.get("models"))
                        if isinstance(data, dict)
                        else None
                    )
                    if not isinstance(entries, list):
                        raise ProtocolError("未识别到模型列表，可手动填写模型名称")
                    models = sorted(
                        {
                            str(x.get("id") or x.get("slug"))
                            for x in entries
                            if isinstance(x, dict)
                            and isinstance(x.get("id") or x.get("slug"), str)
                            and (x.get("id") or x.get("slug")).strip()
                        }
                    )
                    return {
                        "models": models[:2000],
                        "truncated": len(models) > 2000,
                        "message": "模型列表不代表图片能力，请运行 AI 自检。"
                        if models
                        else "服务返回空列表，可手动填写模型名称。",
                    }
    except (httpx.HTTPError, TimeoutError):
        raise ProtocolError("模型列表请求失败或超时，请检查网络和地址") from None
    raise ProtocolError("未找到模型列表接口（404/405），可手动填写模型名称")
