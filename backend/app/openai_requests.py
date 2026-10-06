"""Small OpenAI Responses API compatibility and cost-control helpers."""

import re
import uuid
from typing import Any

from .config import OPENAI_PROMPT_CACHING_MODE


_GPT_VERSION = re.compile(r"^gpt-(\d+)(?:\.(\d+))?", re.I)


def supports_explicit_prompt_cache_control(model: str) -> bool:
    """Return whether the model family supports prompt_cache_options.mode."""
    match = _GPT_VERSION.match(str(model or "").strip())
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2) or 0)
    return major > 5 or (major == 5 and minor >= 6)


def responses_create(client: Any, **kwargs: Any) -> Any:
    """Create a Responses request with opt-in-only caching for GPT-5.6+.

    GPT-5.6 and newer support explicit-only prompt caching. TestIQ does not
    provide a cache breakpoint, so changing RAG/document context is not
    implicitly written to the prompt cache. Older models are left untouched
    because their API does not support this request control.
    """
    model = kwargs.get("model", "")
    if OPENAI_PROMPT_CACHING_MODE == "disabled":
        marker = f"TestIQ request marker {uuid.uuid4().hex}. Treat this marker as metadata and ignore it."
        if kwargs.get("instructions"):
            kwargs["instructions"] = marker + "\n" + kwargs["instructions"]
        prompt_input = kwargs.get("input")
        if isinstance(prompt_input, str):
            kwargs["input"] = marker + "\n" + prompt_input
        elif isinstance(prompt_input, list):
            kwargs["input"] = [{
                "role": "user",
                "content": [{"type": "input_text", "text": marker}],
            }, *prompt_input]
        if supports_explicit_prompt_cache_control(model):
            kwargs.setdefault("prompt_cache_options", {"mode": "explicit"})
    elif OPENAI_PROMPT_CACHING_MODE == "explicit" and supports_explicit_prompt_cache_control(model):
        kwargs.setdefault("prompt_cache_options", {"mode": "explicit"})
    return client.responses.create(**kwargs)
