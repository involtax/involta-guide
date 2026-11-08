from decimal import Decimal

import pytest

from involta_guide.infrastructure.ai.cost_estimate import estimate_cost


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-6-astra", "13.2"),
        ("gpt-6.1-sol", "2.62"),
        ("gpt-6.1-terra", "1.31"),
        ("gpt-6-luna", "0.132"),
        ("gpt-5.2", "2.835"),
        ("gpt-4.1", "2.5"),
    ],
)
def test_standard_rates_deduct_cached_input_and_do_not_add_reasoning_twice(model, expected):
    result = estimate_cost(
        {"model": model},
        {
            "usage": {
                "input_tokens": 1000000,
                "input_tokens_details": {"cached_tokens": 200000},
                "output_tokens": 100000,
                "output_tokens_details": {"reasoning_tokens": 90000},
            }
        },
    )
    assert result["available"] and result["currency"] == "USD"
    assert Decimal(result["amount"]) == Decimal(expected)


@pytest.mark.parametrize("model,search_cost", [("gpt-4.1", "0.050"), ("gpt-6.1-sol", "0.020")])
def test_web_search_preview_calls_are_added_once(model, search_cost):
    response = {
        "usage": {"input_tokens": 100, "output_tokens": 200},
        "output": [
            {"type": "web_search_call", "id": "one"},
            {"type": "web_search_call", "id": "two"},
            {"type": "message"},
        ],
    }
    first = estimate_cost({"model": model, "tools": [{"type": "web_search_preview"}]}, response)
    assert first == estimate_cost(
        {"model": model, "tools": [{"type": "web_search_preview"}]}, response
    )
    assert first["web_search_calls"] == 2
    assert Decimal(first["web_search_amount"]) == Decimal(search_cost)
    assert Decimal(first["amount"]) == Decimal(first["token_amount"]) + Decimal(search_cost)
    assert not first["cache_known"]


@pytest.mark.parametrize(
    "request_body,response",
    [
        ({"model": "gpt-4.1-mini"}, {"usage": {"input_tokens": 1, "output_tokens": 1}}),
        ({"model": "unknown"}, {"usage": {"input_tokens": 1, "output_tokens": 1}}),
        ({"model": "gpt-4.1"}, {"usage": None}),
        (
            {"model": "gpt-4.1", "service_tier": "priority"},
            {"usage": {"input_tokens": 1, "output_tokens": 1}},
        ),
        (
            {"model": "gpt-4.1"},
            {
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 2},
                }
            },
        ),
        (
            {"model": "gpt-6.1-sol"},
            {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "input_tokens_details": {"cache_write_tokens": 5},
                }
            },
        ),
        (
            {"model": "gpt-4.1", "tools": [{"type": "code_interpreter"}]},
            {"usage": {"input_tokens": 1, "output_tokens": 1}},
        ),
    ],
)
def test_missing_usage_and_unsupported_tariffs_never_produce_a_fake_zero(request_body, response):
    result = estimate_cost(request_body, response)
    assert not result["available"] and result["reason"]
    assert "amount" not in result


def test_response_snapshot_model_wins_over_requested_model_and_tiny_cost_is_not_zero():
    result = estimate_cost(
        {"model": "gpt-6.1-sol"},
        {
            "model": "gpt-4.1-2025-04-14",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    assert Decimal(result["amount"]) == Decimal("0.000010")
    assert result["display"] == "<$0.0001"
