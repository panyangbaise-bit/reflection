import json
import logging
from datetime import timedelta

from portfolio_backend.db import now
from portfolio_backend.models import AgentToolCall

logger = logging.getLogger(__name__)
SAFE_ARGUMENTS = {
    "ticker",
    "market",
    "offset",
    "limit",
    "days",
    "priority",
    "source_tool_call_id",
    "model",
    "message_count",
    "tool_count",
    "max_output_tokens",
}


def redacted(args):
    return {
        str(k)[:64]: v if k in SAFE_ARGUMENTS and isinstance(v, (str, int)) else "[redacted]"
        for k, v in args.items()
    }


class Trace:
    def __init__(self, db, claim, cfg=None):
        self.db, self.claim, self.cfg = db, claim, cfg

    def __call__(self, event):
        try:
            with self.db.tenant(self.claim["user_id"]) as s:
                s.add(
                    AgentToolCall(
                        user_id=self.claim["user_id"],
                        run_id=self.claim["payload"].get("run_id"),
                        job_id=self.claim["id"],
                        triggered_by=self.claim["kind"],
                        round=event["round"],
                        tool_name=event["name"][:100],
                        call_id=event["call_id"][:200],
                        status=event["status"],
                        arguments=redacted(event["args"]),
                        result_summary=event["summary"][:1000],
                        content=bounded_content(event.get("content"))
                        if self.cfg and self.cfg.agent_trace_capture_content
                        else None,
                        error_code=event.get("error"),
                        duration_ms=event["duration_ms"],
                        finished_at=now(),
                        created_at=now() - timedelta(milliseconds=event["duration_ms"]),
                    )
                )
        except Exception:
            logger.warning("tool_trace_write_failed job_id=%s", self.claim["id"])


def bounded_content(value):
    """Bound stored content and exclude credentials and model reasoning fields."""
    if value is None:
        return None

    def clean(item):
        if isinstance(item, dict):
            return {
                str(k): "[redacted]"
                if any(
                    part in str(k).lower()
                    for part in (
                        "password",
                        "authorization",
                        "api_key",
                        "access_token",
                        "refresh_token",
                        "credential",
                        "reasoning_content",
                        "thinking",
                    )
                )
                else clean(v)
                for k, v in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        return item

    data = clean(value)
    raw = json.dumps(data, ensure_ascii=False, default=str)
    if len(raw) > 100000:
        return {"truncated": True, "original_chars": len(raw), "preview": raw[:100000]}
    return json.loads(raw)
