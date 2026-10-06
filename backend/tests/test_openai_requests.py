from types import SimpleNamespace

from app import openai_requests


class _FakeResponses:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs


def test_explicit_prompt_cache_control_is_limited_to_gpt_56_plus(monkeypatch):
    monkeypatch.setattr(openai_requests, "OPENAI_PROMPT_CACHING_MODE", "explicit")
    responses = _FakeResponses()
    client = SimpleNamespace(responses=responses)

    openai_requests.responses_create(client, model="gpt-5.6", input="large context")
    openai_requests.responses_create(client, model="gpt-6.1-sol", input="large context")
    openai_requests.responses_create(client, model="gpt-5.5", input="large context")
    openai_requests.responses_create(client, model="gpt-4o-mini", input="large context")

    assert responses.calls[0]["prompt_cache_options"] == {"mode": "explicit"}
    assert responses.calls[1]["prompt_cache_options"] == {"mode": "explicit"}
    assert "prompt_cache_options" not in responses.calls[2]
    assert "prompt_cache_options" not in responses.calls[3]


def test_disabled_mode_adds_unique_marker_and_explicit_mode_for_gpt_56(monkeypatch):
    monkeypatch.setattr(openai_requests, "OPENAI_PROMPT_CACHING_MODE", "disabled")
    responses = _FakeResponses()
    client = SimpleNamespace(responses=responses)

    openai_requests.responses_create(client, model="gpt-5.6", instructions="Stable instructions", input="Changing context")
    openai_requests.responses_create(client, model="gpt-4o-mini", input=[{"role": "user", "content": "Changing context"}])

    first, second = responses.calls
    assert first["prompt_cache_options"] == {"mode": "explicit"}
    assert first["instructions"].startswith("TestIQ request marker ")
    assert first["input"].startswith("TestIQ request marker ")
    assert second["input"][0]["content"][0]["text"].startswith("TestIQ request marker ")
