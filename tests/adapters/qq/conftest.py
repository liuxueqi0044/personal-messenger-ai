from __future__ import annotations

import asyncio
import inspect


def pytest_pyfunc_call(pyfuncitem):
    """Keep QQ fake tests runnable without the optional pytest-asyncio extra."""
    if inspect.iscoroutinefunction(
        pyfuncitem.obj
    ) and not pyfuncitem.config.pluginmanager.hasplugin("asyncio"):
        kwargs = {
            name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames
        }
        asyncio.run(pyfuncitem.obj(**kwargs))
        return True
    return None
