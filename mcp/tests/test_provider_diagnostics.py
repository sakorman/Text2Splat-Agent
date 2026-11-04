"""Fault injection only: no live model requests or credentials."""
import httpx
import pytest

from graphics_mcp.providers import ProviderError, Providers
from graphics_mcp.settings import Settings


@pytest.mark.parametrize("operation", ["plan", "vision", "image"])
@pytest.mark.parametrize("status,code,expected", [
    (429, "1305", "暂时繁忙"),
    (429, "1302", "请求频率"),
    (429, "1113", "账户欠费"),
    (403, "1220", "访问权限"),
    (429, "1311", "套餐未开放"),
])
def test_provider_failure_distinguishes_cause_without_retry(tmp_path, operation, status, code, expected):
    calls = []

    def reject(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": code, "message": "private-provider-body"}})

    provider = Providers(Settings(data_dir=tmp_path, zhipu_key="test-secret"),
                         httpx.Client(transport=httpx.MockTransport(reject)))
    with pytest.raises(ProviderError) as caught:
        if operation == "plan":
            provider.plan("青瓷花瓶")
        elif operation == "vision":
            provider.chat_json("glm-4.6v-flash", [{"role": "user", "content": "明确的视觉审核替身"}])
        else:
            provider.image("青瓷花瓶")
    message = str(caught.value)
    assert expected in message and code in message and "未自动重试" in message
    assert "private-provider-body" not in message and "test-secret" not in message
    assert len(calls) == 1


@pytest.mark.parametrize("body", [
    {"error": {"code": "unknown-private-code", "message": "账户欠费 private-provider-body"}},
    {"error": {"code": "1113\nprivate-provider-body", "message": "账户欠费"}},
    {"error": {"code": ["1113"], "message": "账户欠费"}},
    {"error": {"code": True, "message": "账户欠费"}},
    {"error": "账户欠费 private-provider-body"},
    {"code": "1113", "message": "账户欠费 private-provider-body"},
    ["1113", "账户欠费 private-provider-body"],
    None,
])
def test_unknown_or_malformed_error_does_not_infer_balance_from_429(tmp_path, body):
    provider = Providers(Settings(data_dir=tmp_path, zhipu_key="test-secret"),
                         httpx.Client(transport=httpx.MockTransport(
                             lambda request: httpx.Response(429, json=body))))
    with pytest.raises(ProviderError) as caught:
        provider.image("青瓷花瓶")
    message = str(caught.value)
    assert "无法判断余额" in message and "HTTP 429" in message
    assert "账户欠费" not in message and "private" not in message


def test_zhipu_business_code_mapping_is_scoped_to_provider_and_http_status(tmp_path):
    for provider_name, status in [("seedream", 429), ("cogview", 503)]:
        provider = Providers(Settings(data_dir=tmp_path, zhipu_key="test-secret", image_provider=provider_name,
                                      ark_key="test-secret", allow_paid=True),
                             httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
                                 status, json={"error": {"code": "1113", "message": "private-provider-body"}}))))
        with pytest.raises(ProviderError) as caught:
            provider.image("青瓷花瓶")
        message = str(caught.value)
        assert "账户欠费" not in message and "1113" not in message and "private" not in message


@pytest.mark.parametrize("content", [b"<html>private-provider-body</html>", b'{"error":',
                                     b'{"error":{"code":"1113","message":"' + b"x" * 65536 + b'"}}',
                                     b"[" * 2000 + b"0" + b"]" * 2000])
def test_non_json_or_oversized_body_keeps_safe_http_diagnostic(tmp_path, content):
    provider = Providers(Settings(data_dir=tmp_path, zhipu_key="test-secret"),
                         httpx.Client(transport=httpx.MockTransport(
                             lambda request: httpx.Response(429, content=content))))
    with pytest.raises(ProviderError) as caught:
        provider.image("青瓷花瓶")
    assert "无法判断余额" in str(caught.value) and "private" not in str(caught.value)
