"""What the provider puts in the `reasoning` request field for each budget
setting: a positive budget asks for thinking, 0 stays silent, -1 turns it off.

    python -m pytest tests/test_reasoning_param.py -v
"""
from app.providers.openai_compatible import OpenAICompatibleProvider


def _body(reasoning_max_tokens, api_mode="chat", max_tokens=1000):
    provider = OpenAICompatibleProvider(
        "https://openrouter.ai/api/v1", "k", "deepseek/deepseek-v4-flash-0731",
        api_mode, reasoning_max_tokens,
    )
    body = {"max_tokens": max_tokens}
    provider._apply_reasoning_budget(body)
    return body


def test_zero_sends_nothing():
    """Ollama and other providers reject unknown fields. Sending 0 must not add a `reasoning` field."""
    assert "reasoning" not in _body(0)


def test_positive_budget_adds_thinking_tokens():
    body = _body(500)
    assert body["reasoning"] == {"max_tokens": 500}
    # the story output keeps its own full budget on top of the thinking budget
    assert body["max_tokens"] == 1500


def test_negative_turns_reasoning_off():
    body = _body(-1)
    assert body["reasoning"] == {"effort": "none"}
    # "off" must not inflate the output budget
    assert body["max_tokens"] == 1000


def test_off_is_not_merely_excluded():
    """`exclude: true` still generates and bills for reasoning tokens. The off setting must omit the field entirely instead of relying on `exclude`."""
    assert _body(-1)["reasoning"].get("exclude") is None


def test_completion_mode_never_sends_reasoning():
    for budget in (-1, 0, 500):
        assert "reasoning" not in _body(budget, api_mode="completion")
