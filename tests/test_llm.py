"""LLM tool loop, provider fallback, timeout and reasoning capture (offline, scripted)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from magbot import tools
from magbot.config import Config, ProviderConfig
from magbot.identity import User
from magbot.llm import LLM, NO_TEXT, NO_VISION, LLMError
from magbot.tools import ToolContext


@pytest.fixture(autouse=True)
def isolated_registry():
    """Empty the tool registry for the test, then restore the real one."""
    saved = dict(tools.REGISTRY)
    tools.REGISTRY.clear()
    yield
    tools.REGISTRY.clear()
    tools.REGISTRY.update(saved)


@pytest.fixture
def register():
    def add(name, func, parameters=None):
        params = parameters or {"type": "object", "properties": {}}
        tools.tool(name, f"test tool {name}", params)(func)
    return add


@pytest.fixture
def config():
    cfg = Config()
    cfg.llm.providers = [ProviderConfig(name="p1", model="m1"),
                         ProviderConfig(name="p2", model="m2")]
    cfg.llm.max_tool_steps = 3
    cfg.llm.timeout = 5
    return cfg


@pytest.fixture
def ctx(config):
    return ToolContext(config=config, user=User("u1", "alice#1001", "alice"),
                       is_admin=False, can_shell=False, channel_id="c1", memory=None)


class Fake:
    """Client factory that replays scripted steps per provider and records requests.

    A step is a response object, an exception to raise, or an async callable.
    """

    def __init__(self, scripts: dict[str, list]):
        self.scripts = {name: list(steps) for name, steps in scripts.items()}
        self.requests: list[tuple[str, dict]] = []
        self.built: list[str] = []

    def __call__(self, provider: ProviderConfig):
        self.built.append(provider.name)

        async def create(**kwargs):
            self.requests.append((provider.name, kwargs))
            step = self.scripts[provider.name].pop(0)
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                return await step()
            return step

        return NS(chat=NS(completions=NS(create=create)))

    def kwargs(self, index: int) -> dict:
        return self.requests[index][1]


def reply_msg(content=None, tool_calls=(), usage=None, **extra):
    message = NS(role="assistant", content=content, tool_calls=list(tool_calls) or None, **extra)
    return NS(choices=[NS(message=message)], usage=usage)


def call(call_id: str, name: str, arguments: str = "{}"):
    return NS(id=call_id, type="function", function=NS(name=name, arguments=arguments))


MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


async def test_plain_answer(config, ctx):
    fake = Fake({"p1": [reply_msg("hello there")]})
    result = await LLM(config, fake).respond(MESSAGES, ctx)
    assert result.text == "hello there"
    assert result.provider == "p1"
    assert result.tool_calls == 0
    assert result.reasoning == ""
    assert fake.kwargs(0)["model"] == "m1"


async def test_one_tool_round(config, ctx, register):
    async def echo(ctx, x):
        return f"got {x}"

    register("t_echo", echo, {"type": "object", "properties": {"x": {"type": "integer"}}})
    fake = Fake({"p1": [reply_msg(None, [call("c1", "t_echo", '{"x": 1}')]),
                        reply_msg("done")]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)

    assert result.text == "done"
    assert result.tool_calls == 1
    names = [t["function"]["name"] for t in fake.kwargs(0)["tools"]]
    assert names == ["t_echo"]
    second = fake.kwargs(1)["messages"]
    assistant = second[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == ""
    assert assistant["tool_calls"] == [{"id": "c1", "type": "function",
                                        "function": {"name": "t_echo", "arguments": '{"x": 1}'}}]
    assert "reasoning_content" not in assistant
    assert second[3] == {"role": "tool", "tool_call_id": "c1", "content": "got 1"}


async def test_multiple_calls_in_one_step(config, ctx, register):
    async def upper(ctx, word):
        return word.upper()

    register("t_upper", upper, {"type": "object", "properties": {"word": {"type": "string"}}})
    fake = Fake({"p1": [reply_msg(None, [call("a", "t_upper", '{"word": "x"}'),
                                         call("b", "t_upper", '{"word": "y"}')]),
                        reply_msg("both")]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)

    assert result.tool_calls == 2
    tool_turns = [m for m in fake.kwargs(1)["messages"] if m["role"] == "tool"]
    assert tool_turns == [{"role": "tool", "tool_call_id": "a", "content": "X"},
                          {"role": "tool", "tool_call_id": "b", "content": "Y"}]


async def test_unknown_tool_becomes_error_result(config, ctx):
    fake = Fake({"p1": [reply_msg(None, [call("c1", "does_not_exist")]), reply_msg("ok")]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)

    assert result.text == "ok"
    tool_turn = fake.kwargs(1)["messages"][-1]
    assert tool_turn["role"] == "tool"
    assert tool_turn["content"].startswith("error:")


async def test_max_tool_steps_forces_final_call_without_tools(config, ctx, register):
    async def ping(ctx):
        return "pong"

    register("t_ping", ping)
    config.llm.max_tool_steps = 2
    fake = Fake({"p1": [reply_msg(None, [call("c1", "t_ping")]),
                        reply_msg(None, [call("c2", "t_ping")]),
                        reply_msg("forced answer")]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)

    assert result.text == "forced answer"
    assert result.tool_calls == 2
    assert len(fake.requests) == 3
    assert "tools" in fake.kwargs(0) and "tools" in fake.kwargs(1)
    assert "tools" not in fake.kwargs(2)


async def test_provider_fallback_uses_next_provider(config, ctx):
    fake = Fake({"p1": [RuntimeError("boom")], "p2": [reply_msg("from two")]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)

    assert result.text == "from two"
    assert result.provider == "p2"
    assert [name for name, _ in fake.requests] == ["p1", "p2"]


async def test_all_providers_failing_raises_llm_error(config, ctx):
    fake = Fake({"p1": [RuntimeError("down")], "p2": [ValueError("also down")]})
    with pytest.raises(LLMError):
        await LLM(config, fake).respond(list(MESSAGES), ctx)


async def test_timeout_raises_llm_error(config, ctx):
    async def slow():
        await asyncio.sleep(1)
        return reply_msg("too late")

    config.llm.timeout = 0.05
    fake = Fake({"p1": [slow]})
    with pytest.raises(LLMError):
        await LLM(config, fake).respond(list(MESSAGES), ctx)


async def test_reasoning_collected_from_extra_fields_and_think_tags(config, ctx, register):
    async def echo(ctx):
        return "ok"

    register("t_think", echo)
    first = reply_msg("<think>plan the tool</think>", [call("c1", "t_think")],
                      model_extra={"reasoning_content": "deepseek thoughts"})
    final = reply_msg("answer <think>hidden</think>visible", reasoning_content="attr thoughts")
    fake = Fake({"p1": [first, final]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)

    assert result.text == "answer visible"
    assert result.reasoning == "deepseek thoughts\n\nplan the tool\n\nattr thoughts\n\nhidden"
    # The echoed assistant turn keeps DeepSeek's reasoning and drops the inline think block.
    assistant = fake.kwargs(1)["messages"][2]
    assert assistant["reasoning_content"] == "deepseek thoughts"
    assert assistant["content"] == ""


async def test_unclosed_think_block_is_reasoning(config, ctx):
    fake = Fake({"p1": [reply_msg("visible<think>still thinking")]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)

    assert result.text == "visible"
    assert result.reasoning == "still thinking"


async def test_vision_downgrade_is_per_provider(config, ctx):
    config.llm.providers[0].vision = False
    config.llm.providers[1].vision = True
    images = [{"role": "system", "content": "sys"},
              {"role": "user", "content": [
                  {"type": "text", "text": "look"},
                  {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    fake = Fake({"p1": [RuntimeError("no")], "p2": [reply_msg("seen")]})
    result = await LLM(config, fake).respond(images, ctx)

    assert result.text == "seen"
    downgraded = fake.kwargs(0)["messages"][1]["content"]
    assert downgraded == [{"type": "text", "text": "look"},
                          {"type": "text", "text": NO_VISION}]
    kept = fake.kwargs(1)["messages"][1]["content"]
    assert kept[1]["type"] == "image_url"
    # The caller's messages are not modified.
    assert images[1]["content"][1]["type"] == "image_url"


async def test_usage_is_summed_across_a_tool_round(config, ctx, register):
    async def ping(ctx):
        return "pong"

    register("t_ping", ping)
    fake = Fake({"p1": [reply_msg(None, [call("c1", "t_ping")],
                                  usage=NS(prompt_tokens=100, completion_tokens=20)),
                        reply_msg("done", usage=NS(prompt_tokens=150, completion_tokens=30))]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)

    assert result.tool_calls == 1
    assert (result.prompt_tokens, result.completion_tokens) == (250, 50)


@pytest.mark.parametrize("usage, expected", [
    (None, (0, 0)),
    (NS(prompt_tokens=None, completion_tokens=None), (0, 0)),
    (NS(prompt_tokens=7, completion_tokens=None), (7, 0)),
])
async def test_usage_none_or_partial_is_tolerated(config, ctx, usage, expected):
    fake = Fake({"p1": [reply_msg("x", usage=usage)]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)

    assert result.text == "x"
    assert (result.prompt_tokens, result.completion_tokens) == expected


async def test_response_without_usage_attribute_is_tolerated(config, ctx):
    bare = NS(choices=[NS(message=NS(role="assistant", content="x", tool_calls=None))])
    fake = Fake({"p1": [bare]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)

    assert result.text == "x"
    assert (result.prompt_tokens, result.completion_tokens) == (0, 0)


async def test_kwargs_omit_unset_options(config, ctx):
    config.llm.temperature = None
    fake = Fake({"p1": [reply_msg("x")]})
    await LLM(config, fake).respond(list(MESSAGES), ctx)

    kwargs = fake.kwargs(0)
    assert set(kwargs) == {"model", "messages", "max_tokens"}
    assert kwargs["max_tokens"] == config.llm.max_tokens


async def test_kwargs_send_temperature_and_reasoning_effort_when_set(config, ctx):
    config.llm.temperature = 0.4
    config.llm.providers[0].reasoning_effort = "low"
    fake = Fake({"p1": [reply_msg("x")]})
    await LLM(config, fake).respond(list(MESSAGES), ctx)

    kwargs = fake.kwargs(0)
    assert kwargs["temperature"] == 0.4
    assert kwargs["reasoning_effort"] == "low"


@pytest.mark.parametrize("content", ["", None])
async def test_empty_text_becomes_placeholder(config, ctx, content):
    fake = Fake({"p1": [reply_msg(content)]})
    result = await LLM(config, fake).respond(list(MESSAGES), ctx)
    assert result.text == NO_TEXT


async def test_clients_are_created_once_per_provider(config, ctx):
    fake = Fake({"p1": [reply_msg("a"), reply_msg("b")]})
    llm = LLM(config, fake)
    await llm.respond(list(MESSAGES), ctx)
    await llm.respond(list(MESSAGES), ctx)
    assert fake.built == ["p1"]


async def test_default_client_unwraps_gateway_envelope():
    """api.cline.bot wraps completions as {"success": true, "data": {...}}."""
    import httpx2
    from magbot.config import ProviderConfig
    from magbot.llm import default_client

    completion = {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi there"}}],
    }
    seen_headers = {}

    def handler(request):
        seen_headers.update(request.headers)
        return httpx2.Response(200, json={"success": True, "data": completion})

    client = default_client(ProviderConfig(base_url="https://gw.example/v1", api_key_env="NONE_SET",
                                           headers={"x-test": "1"}))
    client._client._transport = httpx2.MockTransport(handler)
    resp = await client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}])
    assert resp.choices[0].message.content == "hi there"
    assert seen_headers.get("x-test") == "1"
