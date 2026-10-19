

async def test_tool_call_handles_null_and_hostile_arguments():
    from magbot import tools
    from magbot.config import Config
    from magbot.identity import User
    from magbot.tools import ToolContext

    calls = []

    @tools.tool("t_echo", "echo", {"type": "object", "properties": {"n": {"type": "integer"}}})
    async def t_echo(ctx, n: int = 3):
        calls.append(n)
        return str(n)

    try:
        ctx = ToolContext(config=Config(), user=User("1", "a#0001", "a"), is_admin=False,
                          can_shell=False, channel_id="c", memory=None)
        assert await tools.call("t_echo", None, ctx) == "3"
        assert await tools.call("t_echo", '{"n": null}', ctx) == "3"
        assert (await tools.call("t_echo", "[" * 100000, ctx)).startswith("error: bad arguments")
        assert (await tools.call("t_echo", '{"zzz": 1}', ctx)).startswith("error: bad arguments")
    finally:
        tools.REGISTRY.pop("t_echo", None)
