import json
import time
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from portfolio_backend.agent.citations import resolve
from portfolio_backend.agent.limits import deadline as bounded
from portfolio_backend.agent.registry import SPECS, WRITES
from portfolio_backend.errors import DomainError


class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    rounds: int
    triggered_by: str
    extra_context: dict
    read_only: bool
    citation_sources: dict
    usage: dict
    halted: str
    charts: list


def build_graph(
    model, tools, executor, cfg, deadline, before_call=lambda: None, trace=lambda e: None
):
    bound = model.bind_tools(tools)
    counts, cache, kline_results = {}, {}, {}

    def fallback(state, code):
        data = state.get("extra_context", {})
        total = len(data.get("positions", []))
        text = f"本次 AI 分析未完成（{code}）。已读取服务端快照，共 {total} 条持仓记录。"
        if data.get("complete") is False or data.get("market_data_withheld"):
            text += "行情或汇率不足，不能给出完整估值或期间收益。"
        text += "请查看数据依据，稍后重试；本段为事实降级说明。"
        return {"messages": [AIMessage(content=text)], "halted": code}

    def agent(state):
        if state.get("halted"):
            return fallback(state, state["halted"])
        rounds = state.get("rounds", 0)
        if time.monotonic() >= deadline:
            return fallback(state, "agent_deadline")
        if rounds >= cfg.agent_max_rounds:
            return fallback(state, "round_limit")
        usage = dict(state.get("usage", {"input_tokens": 0, "output_tokens": 0}))
        # Upper bound input by UTF-8 bytes; reserve full output before each paid call.
        # Include schemas and ephemeral reasoning when enforcing the session budget.
        estimated = len(
            json.dumps(
                [m.model_dump() for m in state["messages"]], default=str, ensure_ascii=False
            ).encode()
        ) + sum(len(json.dumps(t.args).encode()) + len(t.description.encode()) for t in tools)
        remaining = cfg.agent_max_tokens - sum(usage.values()) - estimated
        if remaining < 128:
            return fallback(state, "token_budget")
        before_call()  # consent/fence/account checked before every external transmission
        start = time.monotonic()
        llm_args = {
            "model": "local-simulator" if cfg.simulator_ai else cfg.deepseek_model,
            "message_count": len(state["messages"]),
            "tool_count": len(tools),
            "max_output_tokens": min(cfg.agent_max_output_tokens, remaining),
        }
        llm_input = (
            [
                {
                    "role": message.type,
                    "content": message.content,
                    **({"tool_call_id": message.tool_call_id} if message.type == "tool" else {}),
                    **({"tool_calls": message.tool_calls} if message.type == "ai" else {}),
                }
                for message in state["messages"]
            ]
            if cfg.agent_trace_capture_content
            else None
        )
        try:
            with bounded(deadline - start):
                answer = bound.invoke(
                    state["messages"],
                    timeout=max(0.1, deadline - start),
                    max_tokens=min(cfg.agent_max_output_tokens, remaining),
                )
        except DomainError as exc:
            trace(
                {
                    "round": rounds + 1,
                    "name": "llm",
                    "call_id": f"llm-{rounds + 1}",
                    "status": "failed",
                    "args": llm_args,
                    "summary": "{}",
                    "content": {"input": llm_input} if llm_input is not None else None,
                    "error": exc.code,
                    "duration_ms": int((time.monotonic() - start) * 1000),
                }
            )
            if exc.code in {"account_unavailable", "ai_consent_required", "stale_worker"}:
                raise
            return fallback(state, exc.code)
        for key in usage:
            usage[key] += (answer.usage_metadata or {}).get(key, 0)
        trace(
            {
                "round": rounds + 1,
                "name": "llm",
                "call_id": f"llm-{rounds + 1}",
                "status": "succeeded",
                "args": llm_args,
                "summary": json.dumps(
                    {
                        "usage": answer.usage_metadata or {},
                        "output_chars": len(str(answer.content)),
                        "tool_calls": [call["name"] for call in answer.tool_calls],
                    }
                ),
                "content": {
                    "input": llm_input,
                    "output": {"content": answer.content, "tool_calls": answer.tool_calls},
                }
                if llm_input is not None
                else None,
                "duration_ms": int((time.monotonic() - start) * 1000),
            }
        )
        if not answer.tool_calls:
            answer = answer.model_copy(
                update={"content": resolve(answer.content, state.get("citation_sources", {}))}
            )
        return {"messages": [answer], "rounds": rounds + 1, "usage": usage}

    def invoke_tools(state):
        out, sources, charts = (
            [],
            dict(state.get("citation_sources", {})),
            list(state.get("charts", [])),
        )
        halted = ""
        for index, call in enumerate(state["messages"][-1].tool_calls):
            name, args, cid = call["name"], call["args"], call["id"]
            started = time.monotonic()
            status = "succeeded"
            try:
                if index >= 16:
                    raise DomainError("tool_batch_limit", "单轮工具数超限")
                if started >= deadline:
                    raise DomainError("agent_deadline", "本次时间用完")
                if state.get("read_only") and name in WRITES:
                    raise DomainError("read_only", "本次仅允许读取")
                if name not in SPECS:
                    raise DomainError("unknown_tool", "未知工具")
                params = SPECS[name][0].model_validate(args).model_dump()
                fingerprint = name + json.dumps(params, sort_keys=True, default=str)
                counts[fingerprint] = counts.get(fingerprint, 0) + 1
                if counts[fingerprint] >= cfg.agent_identical_call_limit:
                    raise DomainError("identical_tool_loop", "重复调用已停止")
                before_call()
                if fingerprint in cache and name not in WRITES:
                    payload = cache[fingerprint]
                    status = "cached"
                elif name == "annotate_kline":
                    source = kline_results.get(params["source_tool_call_id"])
                    if (
                        not source
                        or any(a["index"] >= len(source["points"]) for a in params["annotations"])
                        or any(
                            line["start_index"] >= line["end_index"]
                            or line["end_index"] >= len(source["points"])
                            for line in params["trendlines"]
                        )
                    ):
                        raise DomainError("invalid_chart_source", "请先取得本轮完整 K 线")
                    payload = source | params
                    charts.append(payload)
                else:
                    with bounded(min(cfg.agent_tool_timeout_seconds, deadline - time.monotonic())):
                        payload = executor(name, params)
                    if name == "get_kline" and payload.get("points"):
                        payload = payload | {"call_id": cid}
                        kline_results[cid] = payload
                    if name not in WRITES and payload.get("index_complete") is not False:
                        cache[fingerprint] = payload
                if payload.get("error"):
                    status = "failed"

                def collect(value):
                    if isinstance(value, dict):
                        if value.get("source_id"):
                            sources[value["source_id"]] = {
                                "title": value.get("title", value.get("source_type", "数据依据")),
                                "url": value.get("url"),
                            }
                        for v in value.values():
                            collect(v)
                    elif isinstance(value, list):
                        for v in value:
                            collect(v)

                collect(payload)
            except ValidationError:
                payload, status = {"error": "invalid_arguments"}, "failed"
            except DomainError as exc:
                if exc.code in {"account_unavailable", "ai_consent_required", "stale_worker"}:
                    raise
                payload, status = {"error": exc.code}, "failed"
                if exc.code in {"agent_deadline", "identical_tool_loop", "tool_batch_limit"}:
                    halted = exc.code
            except Exception:
                payload, status = {"error": "tool_failed"}, "failed"
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
            truncated = len(encoded) > cfg.agent_result_max_chars
            if truncated:
                encoded = json.dumps(
                    {
                        "truncated": True,
                        "preview": encoded[: cfg.agent_result_max_chars],
                        "instruction": "缩小范围或分页读取，不能视为完整结果",
                    },
                    ensure_ascii=False,
                )
            out.append(ToolMessage(content=encoded, tool_call_id=cid, name=name))
            # Structural summary only: no private free text, monetary values or reasoning.
            trace(
                {
                    "round": state["rounds"],
                    "name": name,
                    "call_id": cid,
                    "args": args,
                    "status": status,
                    "error": payload.get("error"),
                    "summary": json.dumps(
                        {
                            "keys": list(payload),
                            "chars": len(encoded),
                            "items": len(payload.get("items", [])),
                            "truncated": truncated,
                        }
                    ),
                    "content": {"input": args, "output": payload}
                    if cfg.agent_trace_capture_content
                    else None,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
        return {"messages": out, "citation_sources": sources, "charts": charts, "halted": halted}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", invoke_tools)
    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent", lambda s: "tools" if s["messages"][-1].tool_calls and not s.get("halted") else END
    )
    graph.add_edge("tools", "agent")
    return graph.compile()
