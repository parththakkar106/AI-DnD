"""Stand-ins for the parts of the app a test must not really call.

Import these rather than writing another copy. Nine test modules each carried
their own `ScriptedProvider`, and the copies had drifted into four different
feature sets, so a test that needed to raise a provider error had to be written
in one of the files whose copy supported it.
"""


class ScriptedProvider:
    """Streams canned replies in place of `OpenAICompatibleProvider`.

    Set `replies` to the texts the model returns, one per call. The last entry
    repeats once the list runs out, so a test that plays more turns than it
    scripted still gets text. To drive the provider-error path, put an
    `Exception` in the list. It is raised rather than streamed.

    State lives on the class, not on the instance, because the turn engine
    constructs the provider itself and a test never sees the object. The autouse
    `reset_scripted_provider` fixture in `conftest.py` clears it between tests.

    `prompts` records every assembled `(system, story)` pair, which is what a
    test asserts on to check what the model was shown.
    """

    last_usage = None
    replies: list = []
    calls = 0
    prompts: list = []

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts, *, temperature, max_tokens):
        index = min(ScriptedProvider.calls, len(ScriptedProvider.replies) - 1)
        ScriptedProvider.calls += 1
        ScriptedProvider.prompts.append((parts.system, parts.story))
        reply = ScriptedProvider.replies[index]
        if isinstance(reply, Exception):
            raise reply
        yield ("text", reply)
