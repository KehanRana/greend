import asyncio
"""HTTP adapter for the explicitly simulated OpenAI-compatible endpoint."""
import httpx
from greenroute.contracts import AdapterResult


class UpstreamFailure(Exception):
    def __init__(self, status, message, code, uncertain=False, retry_after=None,
                 safe_to_retry=False, endpoint_failure=False, disable_endpoint=False):
        self.status = status
        self.message = message
        self.code = code
        self.uncertain = uncertain
        self.retry_after = retry_after
        self.safe_to_retry = safe_to_retry
        self.endpoint_failure = endpoint_failure
        self.disable_endpoint = disable_endpoint


async def execute(client, settings, region, payload, request_id):
    return await execute_http(client, f"{settings.upstream_url}/{region}/v1/chat/completions",
                              settings.upstream_key.get_secret_value(), payload.model_dump(exclude_none=True),
                              request_id, settings.timeout_seconds)


async def execute_http(client, url, api_key, body, request_id, timeout_seconds):
    """Shared completion/error contract; callers supply administrator-approved destinations."""
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await client.post(
                url,
                json=body, follow_redirects=False,
                headers={"authorization": f"Bearer {api_key}",
                         "x-greenroute-request-id": request_id})
    except httpx.PoolTimeout:
        raise UpstreamFailure(502, "Local upstream connection pool is busy", "upstream_pool_busy",
                              safe_to_retry=True) from None
    except (httpx.ConnectTimeout, httpx.ConnectError):
        raise UpstreamFailure(502, "Unable to connect to upstream", "upstream_unavailable",
                              safe_to_retry=True, endpoint_failure=True) from None
    except (httpx.TimeoutException, TimeoutError):
        raise UpstreamFailure(504, "Upstream timed out; completion is uncertain", "upstream_timeout", uncertain=True,
                              endpoint_failure=True) from None
    except httpx.RequestError:
        raise UpstreamFailure(502, "Upstream connection interrupted; completion is uncertain", "upstream_interrupted",
                              uncertain=True, endpoint_failure=True) from None
    if response.status_code == 429:
        retry = response.headers.get("retry-after", "")
        raise UpstreamFailure(429, "Upstream rate limit", "upstream_rate_limit",
                              retry_after=retry or None, safe_to_retry=True)
    if response.is_error or response.is_redirect:
        raise UpstreamFailure(502, "Upstream rejected request", "upstream_error",
                              uncertain=response.status_code >= 500,
                              endpoint_failure=response.status_code >= 500,
                              disable_endpoint=response.status_code in {401,403,404})
    try:
        data = response.json()
        if not (isinstance(data, dict) and isinstance(data.get("choices"), list) and data["choices"]
                and isinstance(data["choices"][0].get("message", {}).get("content"), str)
                and data.get("object") == "chat.completion"
                and isinstance(data.get("id"), str)):
            raise ValueError("Invalid completion")
        raw_usage = data.get("usage")
        usage = None
        if isinstance(raw_usage, dict):
            usage = {k: v for k, v in raw_usage.items() if k in
                     {"prompt_tokens", "completion_tokens", "total_tokens"} and type(v) is int and v >= 0}
        return AdapterResult(completion=data, usage=usage, upstream_request_id=data["id"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise UpstreamFailure(502, "Invalid upstream completion", "invalid_upstream_response", uncertain=True) from None
