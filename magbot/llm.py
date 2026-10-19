"""OpenAI-compatible chat client: provider fallback, tool loop, reasoning capture."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

import httpx2
import openai

from . import tools
from .config import Config, ProviderConfig
from .tools import ToolContext

log = logging.getLogger(__name__)

NO_TEXT = "(no text response)"
NO_VISION = "[image attached, but this model can't see images]"
_THINK = re.compile(r"<think>(.*?)(?:</think>|\Z)", re.DOTALL)

ClientFactory = Callable[[ProviderConfig], Any]


@dataclass
class Reply:
    text: str
    reasoning: str = ""
    provider: str = ""
    tool_calls: int = 0
    prompt_tokens: int = 0       # summed over every model step in the reply
    completion_tokens: int = 0


class LLMError(Exception):
    """The model could not produce a reply (all providers failed, or timed out)."""


async def _unwrap_envelope(response: httpx2.Response) -> None:
    """Some gateways (e.g. api.cline.bot) wrap the completion as
    {"success": true, "data": {...}}; hand the SDK the inner completion."""
    if response.status_code != 200:
        return
    await response.aread()
    try:
        body = response.json()
    except ValueError:
        return
    if (isinstance(body, dict) and body.get("success") is True
            and isinstance(body.get("data"), dict) and "choices" in body["data"]):
        response._content = json.dumps(body["data"]).encode()


def default_client(provider: ProviderConfig, timeout: float = 45.0) -> Any:
    http = httpx2.AsyncClient(timeout=timeout, event_hooks={"response": [_unwrap_envelope]})
    return openai.AsyncOpenAI(base_url=provider.base_url, api_key=provider.api_key or "none",
                              timeout=timeout, max_retries=1, http_client=http,
                              default_headers=provider.headers or None)


class LLM:
    def __init__(self, config: Config, client_factory: ClientFactory | None = None):
        self.config = config
        self._factory = client_factory or (
            lambda p: default_client(p, config.llm.request_timeout))
        self._clients: dict[int, Any] = {}

    async def respond(self, messages: list[dict], ctx: ToolContext) -> Reply:
        """Run the tool loop until the model gives a text answer, within the timeout."""
        try:
            async with asyncio.timeout(self.config.llm.timeout):
                return await self._converse(messages, ctx)
        except TimeoutError as e:
            log.warning("LLM reply timed out after %ss", self.config.llm.timeout)
            raise LLMError("the reply timed out") from e

    async def _converse(self, messages: list[dict], ctx: ToolContext) -> Reply:
        convo = list(messages)
        schemas = [t.schema() for t in tools.available(ctx)]
        max_steps = self.config.llm.max_tool_steps
        reasoning: list[str] = []
        calls_made = 0
        step = 0
        prompt_tokens = completion_tokens = 0
        while True:
            # After max_tool_steps rounds, the last call is offered no tools,
            # which forces a text answer.
            forced = step >= max_steps
            msg, provider, usage = await self._complete(convo, None if forced else schemas)
            prompt_tokens += _field(usage, "prompt_tokens") or 0
            completion_tokens += _field(usage, "completion_tokens") or 0
            text, thoughts = _split_reasoning(msg)
            reasoning.extend(thoughts)
            calls = [] if forced else _function_calls(msg)
            if not calls:
                log.info("LLM reply via %s after %d tool call(s), %d prompt + %d completion tokens",
                         provider.name, calls_made, prompt_tokens, completion_tokens)
                return Reply(text=text or NO_TEXT, reasoning="\n\n".join(reasoning),
                             provider=provider.name, tool_calls=calls_made,
                             prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
            convo.append(_assistant_message(msg, text, calls))
            for call in calls:
                result = await tools.call(call.function.name, call.function.arguments, ctx)
                convo.append({"role": "tool", "tool_call_id": call.id, "content": result})
            calls_made += len(calls)
            step += 1

    async def _complete(self, messages: list[dict], offer: list[dict] | None):
        """One model step: try each provider in order.

        Returns (message, provider, usage). usage is the SDK's usage object, or None.
        """
        providers = self.config.llm.providers
        if not providers:
            raise LLMError("no LLM providers configured")
        for index, provider in enumerate(providers):
            try:
                client = self._client(index, provider)
                kwargs = self._kwargs(provider, messages, offer)
                resp = await client.chat.completions.create(**kwargs)
                return resp.choices[0].message, provider, getattr(resp, "usage", None)
            except Exception as e:
                log.warning("LLM provider %s failed: %s: %s", provider.name,
                            type(e).__name__, e)
        raise LLMError("all LLM providers failed")

    def _client(self, index: int, provider: ProviderConfig) -> Any:
        if index not in self._clients:
            self._clients[index] = self._factory(provider)
        return self._clients[index]

    def _kwargs(self, provider: ProviderConfig, messages: list[dict],
                offer: list[dict] | None) -> dict:
        llm = self.config.llm
        kwargs: dict[str, Any] = {
            "model": provider.model,
            "messages": messages if provider.vision else _without_images(messages),
            "max_tokens": llm.max_tokens,
        }
        if llm.temperature is not None:
            kwargs["temperature"] = llm.temperature
        if provider.reasoning_effort:
            kwargs["reasoning_effort"] = provider.reasoning_effort
        if offer:
            kwargs["tools"] = offer
        return kwargs


def _field(obj: Any, name: str) -> Any:
    """An attribute, or an extra field the SDK kept in model_extra."""
    value = getattr(obj, name, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(name)
    return value


def _function_calls(msg: Any) -> list[Any]:
    """Tool calls we can answer (custom tool-call types have no .function)."""
    return [c for c in (getattr(msg, "tool_calls", None) or [])
            if getattr(c, "function", None) is not None]


def _split_reasoning(msg: Any) -> tuple[str, list[str]]:
    """Return (visible text, reasoning pieces) for one model message."""
    thoughts: list[str] = []
    extra = _field(msg, "reasoning_content")
    if extra:
        thoughts.append(str(extra).strip())
    content = getattr(msg, "content", None) or ""
    thoughts += [t.strip() for t in _THINK.findall(content) if t.strip()]
    return _THINK.sub("", content).strip(), [t for t in thoughts if t]


def _assistant_message(msg: Any, text: str, calls: list[Any]) -> dict:
    """Serialise an assistant turn that requested tools, for the next request."""
    out: dict[str, Any] = {
        "role": "assistant",
        "content": text,
        "tool_calls": [{"id": c.id, "type": "function",
                        "function": {"name": c.function.name,
                                     "arguments": c.function.arguments}} for c in calls],
    }
    reasoning = _field(msg, "reasoning_content")
    if reasoning:
        # DeepSeek-style providers reject tool turns without their reasoning.
        out["reasoning_content"] = reasoning
    return out


def _without_images(messages: list[dict]) -> list[dict]:
    """Copy of messages with image parts replaced by a text note."""
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts = [{"type": "text", "text": NO_VISION} if p.get("type") == "image_url" else p
                     for p in content]
            message = {**message, "content": parts}
        out.append(message)
    return out
