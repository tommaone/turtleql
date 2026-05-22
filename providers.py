"""
LLM provider abstraction for TurtleQL.

Provider selection (first match wins):
  AZURE_OPENAI_ENDPOINT set      → AzureOpenAIProvider
  CLAUDE_CODE_USE_BEDROCK=1      → BedrockProvider
  else                           → AnthropicProvider

AWS profile: AWS_PROFILE env var (default: "default")
CA bundle:   AWS_CA_BUNDLE env var (optional)

Azure deployment names:
  AZURE_OPENAI_LARGE_DEPLOYMENT  → used for MODEL_LARGE requests
  AZURE_OPENAI_SMALL_DEPLOYMENT  → used for MODEL_SMALL requests
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Neutral internal types
# ---------------------------------------------------------------------------

@dataclass
class LLMToolCall:
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class LLMResponse:
    stop_reason: str                        # "tool_calls" | "end_turn"
    text: Optional[str]
    tool_calls: List[LLMToolCall] = field(default_factory=list)
    raw: Any = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Helper: convert neutral tool defs to Anthropic/Bedrock wire format
# ---------------------------------------------------------------------------

def _tools_to_anthropic(tools: List[Dict]) -> List[Dict]:
    """Rename 'parameters' → 'input_schema' for Anthropic/Bedrock."""
    out = []
    for t in tools:
        t2 = dict(t)
        if "parameters" in t2:
            t2["input_schema"] = t2.pop("parameters")
        out.append(t2)
    return out


def _anthropic_response_to_llm(resp: Dict) -> LLMResponse:
    """Map a raw Anthropic/Bedrock response dict to LLMResponse."""
    stop_reason = resp.get("stop_reason", "end_turn")
    if stop_reason == "tool_use":
        stop_reason = "tool_calls"

    text: Optional[str] = None
    tool_calls: List[LLMToolCall] = []

    for block in resp.get("content", []):
        if block.get("type") == "text":
            text = block["text"]
        elif block.get("type") == "tool_use":
            tool_calls.append(LLMToolCall(
                id=block["id"],
                name=block["name"],
                input=block.get("input", {}),
            ))

    return LLMResponse(stop_reason=stop_reason, text=text, tool_calls=tool_calls, raw=resp)


def _build_anthropic_messages(messages: List[Dict]) -> List[Dict]:
    """Translate neutral thread format → Anthropic wire messages."""
    out = []
    for msg in messages:
        role = msg["role"]
        if role == "user":
            out.append({"role": "user", "content": msg["content"]})
        elif role == "assistant":
            content = []
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    content.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    })
            if msg.get("text"):
                content.append({"type": "text", "text": msg["text"]})
            if not content:
                content = [{"type": "text", "text": "..."}]
            out.append({"role": "assistant", "content": content})
        elif role == "tool_results":
            content = []
            for tr in msg["tool_results"]:
                content.append({
                    "type": "tool_result",
                    "tool_use_id": tr["id"],
                    "content": tr["content"],
                })
            out.append({"role": "user", "content": content})
    return out


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class BedrockProvider:
    def complete(self, messages: List[Dict], system: str, tools: List[Dict],
                 model: str, max_tokens: int) -> LLMResponse:
        import boto3

        ca_bundle = Path(os.environ["AWS_CA_BUNDLE"]) if os.environ.get("AWS_CA_BUNDLE") else None
        if ca_bundle and ca_bundle.exists():
            os.environ["AWS_CA_BUNDLE"] = str(ca_bundle)

        profile = os.environ.get("AWS_PROFILE", "default")

        def _client():
            try:
                return boto3.Session(profile_name=profile, region_name="eu-central-1").client("bedrock-runtime")
            except Exception:
                return boto3.client("bedrock-runtime", region_name="eu-central-1")

        bedrock = _client()
        body: Dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": _build_anthropic_messages(messages),
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = _tools_to_anthropic(tools)

        raw = json.loads(bedrock.invoke_model(modelId=model, body=json.dumps(body))["body"].read())
        return _anthropic_response_to_llm(raw)


class AnthropicProvider:
    def complete(self, messages: List[Dict], system: str, tools: List[Dict],
                 model: str, max_tokens: int) -> LLMResponse:
        import requests as _req

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _build_anthropic_messages(messages),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = _tools_to_anthropic(tools)

        r = _req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json=payload,
        )
        r.raise_for_status()
        return _anthropic_response_to_llm(r.json())


class AzureOpenAIProvider:
    def __init__(self):
        from openai import AzureOpenAI
        self._client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        )
        self._large_deployment = os.environ.get("AZURE_OPENAI_LARGE_DEPLOYMENT")
        self._small_deployment = os.environ.get("AZURE_OPENAI_SMALL_DEPLOYMENT")
        self._small_model_id = os.environ.get("TURTLEQL_SMALL_MODEL", "")

    def _deployment(self, model: str) -> str:
        if model == self._small_model_id and self._small_deployment:
            return self._small_deployment
        if self._large_deployment:
            return self._large_deployment
        return model

    def complete(self, messages: List[Dict], system: str, tools: List[Dict],
                 model: str, max_tokens: int) -> LLMResponse:
        wire_messages = _build_openai_messages(messages, system)
        wire_tools = [_tool_to_openai(t) for t in tools] if tools else None

        kwargs: Dict[str, Any] = {
            "model": self._deployment(model),
            "max_completion_tokens": max_tokens,
            "messages": wire_messages,
        }
        if wire_tools:
            kwargs["tools"] = wire_tools

        resp = self._client.chat.completions.create(**kwargs)
        return _openai_response_to_llm(resp)


# ---------------------------------------------------------------------------
# OpenAI wire format helpers
# ---------------------------------------------------------------------------

def _build_openai_messages(messages: List[Dict], system: str) -> List[Dict]:
    out: List[Dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for msg in messages:
        role = msg["role"]
        if role == "user":
            out.append({"role": "user", "content": msg["content"]})
        elif role == "assistant":
            m: Dict[str, Any] = {"role": "assistant"}
            if msg.get("text"):
                m["content"] = msg["text"]
            if msg.get("tool_calls"):
                m["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                    }
                    for tc in msg["tool_calls"]
                ]
            out.append(m)
        elif role == "tool_results":
            for tr in msg["tool_results"]:
                out.append({
                    "role": "tool",
                    "tool_call_id": tr["id"],
                    "content": tr["content"],
                })
    return out


def _tool_to_openai(tool: Dict) -> Dict:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        },
    }


def _openai_response_to_llm(resp) -> LLMResponse:
    choice = resp.choices[0]
    finish_reason = choice.finish_reason
    stop_reason = "tool_calls" if finish_reason == "tool_calls" else "end_turn"

    text: Optional[str] = choice.message.content or None
    tool_calls: List[LLMToolCall] = []

    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            try:
                inp = json.loads(tc.function.arguments)
            except Exception:
                inp = {}
            tool_calls.append(LLMToolCall(id=tc.id, name=tc.function.name, input=inp))

    return LLMResponse(stop_reason=stop_reason, text=text, tool_calls=tool_calls, raw=resp)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_provider():
    if os.environ.get("AZURE_OPENAI_ENDPOINT"):
        return AzureOpenAIProvider()
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        return BedrockProvider()
    return AnthropicProvider()
