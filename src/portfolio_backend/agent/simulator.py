"""Opt-in local fixture model. No external request and no claim of real AI output."""

import json

from langchain_core.messages import AIMessage


class SimulatorModel:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        last = messages[-1]
        if last.type != "tool":
            return AIMessage(
                content="",
                tool_calls=[{"name": "get_portfolio", "args": {}, "id": "local-portfolio"}],
            )
        data = json.loads(last.content)
        return AIMessage(
            content=f"【本机合成联调回答，不是真实模型分析】已通过租户工具查询到 {len(data.get('items', []))} 条持仓。本消息用于验证任务队列、历史会话、报告追问额度及工具轨迹。行情缺失时不推算收益。"
        )
