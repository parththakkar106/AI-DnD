"""Shared setup for the test suite.

pytest imports this file before it imports any test module, which is the only
reason the database redirection below works. `app.database` reads
`AIDND_DB_PATH` at import and builds `engine` from it once, so the variable has
to be set before the first `from app...` line anywhere in the suite.

Every test module used to carry its own copy of that redirection. Only the first
one to be imported ever took effect, because the engine already existed by the
time the second one ran. The other copies created a temp file that nothing
opened and nothing deleted. One copy here does the job, and it cleans up after
itself.

The tests share one database. That is not new: they already did. Each `client`
fixture calls `Base.metadata.create_all` on setup and `drop_all` on teardown, so
no test sees another test's rows.
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
# A real `AIDND_DATABASE_URL` or `DATABASE_URL` in the developer's shell points
# at Postgres, and `app.database` prefers either over the SQLite path above.
# Clear both, so running the suite never touches a server database.
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest  # noqa: E402  Import order is load-bearing; see above.

from fakes import ScriptedProvider  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """A test that pins who the caller is pins both ways of asking.

    There are two: `auth.get_current_user`, which every endpoint that needs an
    account declares, and `auth.get_optional_user`, which the read-only ones
    declare so that someone with no account yet can be answered without writing
    them down. A fixture that overrides only the first leaves the second to
    resolve for real, and the endpoints using it then see a visitor and answer
    with an empty list. That is a confusing failure a long way from its cause,
    and it lands on whichever test happens to read a list.

    So the override is mirrored here, once, rather than in the twenty-odd
    fixtures that set one. `setdefault` means a test that pins the two
    separately, as the lazy-guest tests do by pinning neither, still gets what
    it asked for. This runs after fixtures and before the test body, which is
    the only point where the override is known to exist.
    """
    from app import auth
    from app.main import app

    override = app.dependency_overrides.get(auth.get_current_user)
    if override is not None:
        app.dependency_overrides.setdefault(auth.get_optional_user, override)
    yield


@pytest.fixture(autouse=True)
def reset_scripted_provider():
    """Clears the fake provider's state between tests.

    `ScriptedProvider` keeps its replies and its call count on the class, because
    the code under test constructs the provider itself and a test cannot reach
    the instance. Class state outlives a test, so reset it here rather than
    trusting every fixture to remember.
    """
    ScriptedProvider.replies = []
    ScriptedProvider.calls = 0
    ScriptedProvider.prompts = []
    yield


def pytest_sessionfinish(session, exitstatus):
    """Deletes the temporary database once the run ends."""
    try:
        os.unlink(_tmp.name)
    except OSError:
        # The file is already gone, or Windows still holds a handle on it. It is
        # in the temp directory either way, so leaving it costs nothing.
        pass
