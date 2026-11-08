import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from portfolio_backend.config import Settings
from portfolio_backend.errors import DomainError
from portfolio_backend.providers.ai import DeepSeek


def config(**kwargs):
    return Settings(_env_file=None, deepseek_api_key=SecretStr("test-secret"), **kwargs)


def payload():
    return {
        "context": {"positions": [], "complete": True},
        "instructions": "基于事实回答",
        "messages": [{"role": "user", "content": "分析组合"}],
    }


def response():
    return {
        "model": "deepseek-flash",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": " 当前没有持仓。 ", "reasoning_content": "private"},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 50},
    }


def test_direct_request_and_usage():
    def handle(request):
        assert str(request.url) == DeepSeek.endpoint
        assert request.headers["Authorization"] == "Bearer test-secret"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["max_tokens"] == 2048 and body["stream"] is False
        assert body["thinking"] == {"type": "disabled"}
        assert body["messages"][-1] == payload()["messages"][0]
        assert "tools" not in body
        return httpx.Response(200, json=response())

    answer = DeepSeek(config(), httpx.MockTransport(handle)).answer(payload())
    assert answer.text == "当前没有持仓。"
    assert answer.cached_input_tokens == 50 and answer.input_tokens == 100
    assert "private" not in answer.model_dump_json()


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "ai_credentials_invalid"),
        (403, "ai_credentials_invalid"),
        (402, "ai_balance_exhausted"),
        (400, "ai_request_rejected"),
        (429, "ai_provider_failed"),
        (503, "ai_provider_failed"),
        (302, "ai_request_rejected"),
    ],
)
def test_errors_are_safe_and_no_inline_retries(status, code):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="PRIVATE upstream request body")

    with pytest.raises(DomainError) as error:
        DeepSeek(config(), httpx.MockTransport(handle)).answer(payload())
    assert error.value.code == code
    assert "PRIVATE" not in str(error.value) and len(calls) == 1


@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls"])
def test_incomplete_output_is_not_charged_as_success(finish):
    body = response()
    body["choices"][0]["finish_reason"] = finish
    with pytest.raises(DomainError, match="未完整"):
        DeepSeek(config(), httpx.MockTransport(lambda _: httpx.Response(200, json=body))).answer(
            payload()
        )


def test_no_context_truncation_and_no_silent_vendor_switch():
    data = payload()
    data["messages"][0]["content"] = "x" * 1001
    with pytest.raises(DomainError) as error:
        DeepSeek(config(ai_max_input_chars=1000)).answer(data)
    assert error.value.code == "ai_context_too_large"
    with pytest.raises(ValidationError):
        config(ai_provider_id="someone_else")
    with pytest.raises(ValidationError):
        config(ai_gateway_url="https://other.example")


def test_invalid_response_and_timeout():
    with pytest.raises(DomainError) as error:
        DeepSeek(config(), httpx.MockTransport(lambda _: httpx.Response(200, json={}))).answer(
            payload()
        )
    assert error.value.code == "ai_invalid_response"

    def timeout(request):
        raise httpx.ReadTimeout("private", request=request)

    with pytest.raises(DomainError) as error:
        DeepSeek(config(), httpx.MockTransport(timeout)).answer(payload())
    assert error.value.code == "ai_provider_failed"


def test_market_display_permission_does_not_authorize_ai_processing():
    from portfolio_backend.worker import financial_context

    data = {
        "positions": [
            {
                "id": "private",
                "account_id": "private",
                "symbol": "AAPL",
                "quantity": "2",
                "average_cost": "100",
                "price": "200",
                "market_value_cny": "2800",
            }
        ],
        "market_data_providers": ["quote-vendor", "fx-vendor"],
        "market_value_cny": "2800",
    }
    result = financial_context(data, config())
    assert result["market_data_withheld"] and "market_value_cny" not in result
    assert result["positions"] == [{"symbol": "AAPL", "quantity": "2", "average_cost": "100"}]
    partial = financial_context(data, config(ai_market_data_providers={"quote-vendor"}))
    assert partial["market_data_withheld"]
    allowed = financial_context(
        data, config(ai_market_data_providers={"quote-vendor", "fx-vendor"})
    )
    assert allowed["market_value_cny"] == "2800" and allowed["positions"][0]["price"] == "200"
    assert "account_id" not in allowed["positions"][0]
