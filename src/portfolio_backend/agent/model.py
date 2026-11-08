"""DeepSeek tool binding. Reasoning stays ephemeral and is never persisted in traces."""

import json

import httpx
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from portfolio_backend.errors import DomainError


class ToolModel:
    def __init__(self, cfg, tools=(), transport=None):
        self.cfg, self.tools, self.transport = cfg, tools, transport

    def bind_tools(self, tools):
        return ToolModel(self.cfg, [convert_to_openai_tool(t) for t in tools], self.transport)

    def invoke(self, messages, timeout, max_tokens):
        wire = []
        for m in messages:
            role = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}[m.type]
            row = {"role": role, "content": m.content}
            if role == "tool":
                row["tool_call_id"] = m.tool_call_id
            if role == "assistant":
                if "reasoning_content" in m.additional_kwargs:
                    row["reasoning_content"] = m.additional_kwargs["reasoning_content"]
                if m.tool_calls:
                    row["tool_calls"] = [
                        {
                            "id": t["id"],
                            "type": "function",
                            "function": {
                                "name": t["name"],
                                "arguments": json.dumps(t["args"], ensure_ascii=False),
                            },
                        }
                        for t in m.tool_calls
                    ]
            wire.append(row)
        if len(json.dumps(wire, ensure_ascii=False)) > self.cfg.ai_max_input_chars:
            raise DomainError("ai_context_too_large", "上下文超出本次限制", 422)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout, connect=min(5, timeout)),
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={
                        "Authorization": "Bearer " + self.cfg.deepseek_api_key.get_secret_value()
                    },
                    json={
                        "model": self.cfg.deepseek_model,
                        "messages": wire,
                        "stream": False,
                        "thinking": {"type": "enabled" if self.cfg.agent_thinking else "disabled"},
                        "max_tokens": max_tokens,
                        **({"tools": self.tools} if self.tools else {}),
                    },
                )
            if response.status_code != 200:
                code = (
                    "ai_credentials_invalid"
                    if response.status_code in {401, 403}
                    else "ai_provider_failed"
                )
                raise DomainError(code, "AI 服务暂不可用", 503)
            body = response.json()
            choice = body["choices"][0]
            if choice["finish_reason"] not in {"stop", "tool_calls"}:
                raise DomainError("ai_answer_incomplete", "回答未完整生成", 503)
            raw = choice["message"]
            calls = [
                {
                    "id": t["id"],
                    "name": t["function"]["name"],
                    "args": json.loads(t["function"]["arguments"]),
                }
                for t in raw.get("tool_calls", [])
            ]
            if any(not isinstance(t["args"], dict) for t in calls):
                raise ValueError("invalid_arguments")
            content = raw.get("content") or ""
            if not isinstance(content, str) or (not calls and not content.strip()):
                raise ValueError("empty_or_invalid_answer")
            usage = body["usage"]
            return AIMessage(
                content=content,
                tool_calls=calls,
                additional_kwargs={"reasoning_content": raw.get("reasoning_content", "")},
                usage_metadata={
                    "input_tokens": usage["prompt_tokens"],
                    "output_tokens": usage["completion_tokens"],
                    "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"],
                },
            )
        except DomainError:
            raise
        except httpx.HTTPError as exc:
            raise DomainError("ai_provider_failed", "AI 服务暂不可用", 503) from exc
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise DomainError("ai_invalid_response", "AI 返回格式错误", 503) from exc
