"""Capture per-request LLM token usage and associate it with workflow steps."""

from __future__ import annotations

import contextvars
import functools
import inspect
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

_CURRENT_USAGE = contextvars.ContextVar("testiq_llm_usage", default={})

# USD per 1M tokens, based on OpenAI's published standard rates. Keep model
# names exact so an unknown/renamed model is reported as unpriced instead of
# silently receiving an incorrect estimate.
MODEL_TOKEN_RATES_USD = {
    "gpt-4o-mini": {"input": Decimal("0.15"), "cached_input": Decimal("0.075"), "output": Decimal("0.60")},
    "text-embedding-3-large": {"input": Decimal("0.13"), "cached_input": Decimal("0.13"), "output": Decimal("0")},
}


def estimate_cost_usd(model: str | None, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> Decimal | None:
    """Estimate one call's USD cost; None means this model has no configured rate."""
    rates = MODEL_TOKEN_RATES_USD.get(str(model or "").strip())
    if not rates:
        return None
    input_count = max(0, int(input_tokens or 0))
    cached_count = min(input_count, max(0, int(cached_input_tokens or 0)))
    uncached_count = input_count - cached_count
    return (
        Decimal(uncached_count) * rates["input"]
        + Decimal(cached_count) * rates["cached_input"]
        + Decimal(max(0, int(output_tokens or 0))) * rates["output"]
    ) / Decimal(1_000_000)


def _value(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def record_usage(response: Any, *, model: str | None = None, status: str = "succeeded", error: Exception | None = None,
                 embedding: bool = False) -> None:
    """Queue provider-reported token counts for the enclosing step to persist."""
    context = _CURRENT_USAGE.get()
    if not context.get("stage"):
        return
    usage = _value(response, "usage") if response is not None else None
    if embedding:
        input_tokens = _int(_value(usage, "prompt_tokens"))
        total_tokens = _int(_value(usage, "total_tokens")) or input_tokens
        output_tokens = 0
        cached_tokens = 0
    else:
        input_tokens = _int(_value(usage, "input_tokens"))
        output_tokens = _int(_value(usage, "output_tokens"))
        total_tokens = _int(_value(usage, "total_tokens")) or input_tokens + output_tokens
        details = _value(usage, "input_tokens_details")
        cached_tokens = _int(_value(details, "cached_tokens"))
    context.setdefault("events", []).append({
        "usage_id": f"tok_{uuid.uuid4().hex}", "stage": context.get("stage"),
        "operation_id": context.get("operation_id"), "project_id": context.get("project_id"),
        "package_id": context.get("package_id"), "source_revision_id": context.get("source_revision_id"),
        "analysis_id": context.get("analysis_id"), "generation_id": context.get("generation_id"),
        "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": total_tokens, "cached_input_tokens": cached_tokens, "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def _persist_events(events: list[dict]) -> None:
    if not events:
        return
    try:
        from .db import connection
        with connection() as conn:
            insert_sql = (
                "INSERT INTO llm_token_usage (usage_id,stage,operation_id,project_id,package_id,source_revision_id,analysis_id,generation_id,model,input_tokens,output_tokens,total_tokens,cached_input_tokens,status,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            )
            # psycopg 3 exposes executemany on cursors, not on Connection.
            # Execute each small usage row through the cross-backend adapter.
            for item in events:
                conn.execute(
                    insert_sql,
                    (item["usage_id"], item["stage"], item["operation_id"], item["project_id"], item["package_id"],
                     item["source_revision_id"], item["analysis_id"], item["generation_id"], item["model"], item["input_tokens"],
                     item["output_tokens"], item["total_tokens"], item["cached_input_tokens"], item["status"], item["created_at"]),
                )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Could not persist LLM token usage")


def usage_scope(stage: str, **context):
    """Set metadata for calls made by the current synchronous workflow operation."""
    class Scope:
        def __enter__(self):
            inherited = dict(_CURRENT_USAGE.get() or {})
            self.root = not inherited.get("stage")
            if self.root:
                inherited["events"] = []
            inherited["depth"] = int(inherited.get("depth", 0)) + 1
            inherited.update({key: value for key, value in context.items() if value is not None})
            inherited.setdefault("stage", stage)
            inherited.setdefault("operation_id", f"op_{uuid.uuid4().hex}")
            self.context = inherited
            self.token = _CURRENT_USAGE.set(inherited)
            return inherited

        def __exit__(self, *_):
            events = self.context.get("events", []) if self.root else []
            _CURRENT_USAGE.reset(self.token)
            if self.root:
                _persist_events(events)
    return Scope()


def usage_step(stage: str):
    """Decorate a workflow helper, inferring package/analysis IDs from its arguments."""
    def decorate(function):
        signature = inspect.signature(function)
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            bound = signature.bind_partial(*args, **kwargs)
            values = bound.arguments
            package = values.get("package")
            package_id = values.get("package_id") or _value(package, "package_id")
            project_id = values.get("project_id") or _value(package, "project_id")
            context = {key: values.get(key) for key in ("source_revision_id", "analysis_id", "generation_id")}
            if context.get("analysis_id") or context.get("generation_id"):
                try:
                    from .db import connection
                    with connection() as conn:
                        if context.get("analysis_id"):
                            row = conn.execute("SELECT project_id,package_id FROM requirement_analysis WHERE analysis_id=%s", (context["analysis_id"],)).fetchone()
                        else:
                            row = conn.execute("SELECT project_id,package_id FROM test_case_generation WHERE generation_id=%s", (context["generation_id"],)).fetchone()
                        project_id = project_id or (_value(row, "project_id") if row else None)
                        package_id = package_id or (_value(row, "package_id") if row else None)
                except Exception:
                    pass
            if values.get("index_id") and not package_id:
                try:
                    from .db import connection
                    with connection() as conn:
                        row = conn.execute("SELECT project_id,package_id FROM knowledge_index WHERE index_id=%s", (values["index_id"],)).fetchone()
                        project_id = project_id or (_value(row, "project_id") if row else None)
                        package_id = package_id or (_value(row, "package_id") if row else None)
                except Exception:
                    pass
            if package_id and not project_id:
                try:
                    from .db import connection
                    with connection() as conn:
                        row = conn.execute("SELECT project_id FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
                        project_id = _value(row, "project_id") if row else None
                except Exception:
                    pass
            if "revision_id" in values:
                context["source_revision_id"] = values["revision_id"]
                try:
                    from .db import connection
                    with connection() as conn:
                        row = conn.execute("SELECT d.project_id FROM source_revision r JOIN source_document d ON d.source_id=r.source_id WHERE r.revision_id=%s", (values["revision_id"],)).fetchone()
                        project_id = project_id or (_value(row, "project_id") if row else None)
                except Exception:
                    pass
            with usage_scope(stage, project_id=project_id, package_id=package_id, **context):
                return function(*args, **kwargs)
        return wrapped
    return decorate
