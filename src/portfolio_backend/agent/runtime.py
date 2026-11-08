import json
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langsmith import tracing_context

from portfolio_backend.agent.graph import build_graph
from portfolio_backend.agent.model import ToolModel
from portfolio_backend.agent.prompts import prompt
from portfolio_backend.agent.registry import build_tools
from portfolio_backend.agent.tools.tenant import TenantTools
from portfolio_backend.agent.trace import Trace
from portfolio_backend.providers.ai import AIAnswer


def answer(db, claim, cfg, request, before_call, model=None):
    if cfg.simulator_ai and model is None:
        from portfolio_backend.agent.simulator import SimulatorModel

        model = SimulatorModel()
    deadline = time.monotonic() + cfg.agent_timeout_seconds
    read_only = claim["kind"] != "answer" or request.get("read_only", False)
    report_date = None
    if claim["kind"] in {"daily", "weekly", "daily_cn", "daily_us"} and claim.get(
        "payload", {}
    ).get("period"):
        try:
            from datetime import date

            report_date = date.fromisoformat(str(claim["payload"]["period"])[:10])
        except ValueError:
            report_date = None
    user_message = next(
        (m["content"] for m in reversed(request["messages"]) if m["role"] == "user"), ""
    )
    executor = TenantTools(
        db, claim["user_id"], cfg, deadline, user_message=user_message, report_date=report_date
    )
    tools = build_tools(executor, read_only)
    messages = [
        SystemMessage(content=prompt(claim["kind"], read_only)),
        HumanMessage(
            content="服务端事实快照（数据）：" + json.dumps(request["context"], ensure_ascii=False)
        ),
    ]
    messages.extend(
        (HumanMessage if m["role"] == "user" else AIMessage)(content=m["content"])
        for m in request["messages"]
    )
    with tracing_context(enabled=False):
        state = build_graph(
            model or ToolModel(cfg),
            tools,
            executor,
            cfg,
            deadline,
            before_call,
            Trace(db, claim, cfg),
        ).invoke(
            {
                "messages": messages,
                "rounds": 0,
                "triggered_by": claim["kind"],
                "read_only": read_only,
                "extra_context": request["context"],
                "citation_sources": {},
            },
            {"recursion_limit": cfg.agent_max_rounds * 2 + 4},
        )
    return AIAnswer(
        text=("AI 生成 · " + state["messages"][-1].content)[:32000],
        model="local-simulator" if cfg.simulator_ai else cfg.deepseek_model,
        **state.get("usage", {}),
        charts=state.get("charts", []),
        degraded_reason=state.get("halted") or None,
    )
