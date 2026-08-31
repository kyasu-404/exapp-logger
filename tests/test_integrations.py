from __future__ import annotations

import asyncio

import pytest

from file_control import integrations


class _EnabledState:
    def __init__(self, values: list[bool]):
        self.values = values

    def __await__(self):
        async def get_value():
            return self.values.pop(0) if len(self.values) > 1 else self.values[0]

        return get_value().__await__()


class FakeNextcloud:
    def __init__(self, states: list[bool] | None = None):
        self.states = states or [True]

    @property
    def enabled_state(self):
        return _EnabledState(self.states)


class FakeResources:
    def __init__(self):
        self.deleted_scripts = []
        self.deleted_styles = []
        self.scripts = []
        self.styles = []

    async def delete_script(self, *args):
        self.deleted_scripts.append(args)

    async def delete_style(self, *args):
        self.deleted_styles.append(args)

    async def set_script(self, *args):
        self.scripts.append(args)

    async def set_style(self, *args):
        self.styles.append(args)


class FakeTopMenu:
    async def register(self, *_args):
        return None


class FakeUi:
    def __init__(self):
        self.resources = FakeResources()
        self.top_menu = FakeTopMenu()


@pytest.fixture(autouse=True)
def reset_integration_task():
    integrations._integration_task = None
    yield
    task = integrations._integration_task
    if task is not None and not task.done():
        task.cancel()
    integrations._integration_task = None


async def test_enable_returns_before_registration(monkeypatch):
    started = asyncio.Event()

    async def register(_nc):
        started.set()

    monkeypatch.setattr(integrations, "INTEGRATION_RETRY_DELAYS", (0,))
    monkeypatch.setattr(integrations, "_register_integrations", register)

    assert await integrations.enabled_handler(True, FakeNextcloud()) == ""
    assert not started.is_set()

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started.is_set()


async def test_resource_paths_omit_extensions_expected_by_app_api(monkeypatch):
    async def register_webhooks(_nc):
        return None

    nc = FakeNextcloud()
    nc.ui = FakeUi()
    monkeypatch.setattr(integrations, "_register_current_webhooks", register_webhooks)

    await integrations._register_integrations(nc)

    assert nc.ui.resources.scripts == [("top_menu", "file_control", "js/app")]
    assert nc.ui.resources.styles == [("top_menu", "file_control", "css/app")]
    assert nc.ui.resources.deleted_scripts == [("top_menu", "file_control", "js/app.js")]
    assert nc.ui.resources.deleted_styles == [("top_menu", "file_control", "css/app.css")]


async def test_registration_retries_until_enabled(monkeypatch):
    calls = 0

    async def register(_nc):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(integrations, "INTEGRATION_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(integrations, "_register_integrations", register)

    await integrations._register_integrations_after_enable(FakeNextcloud([False, True]))

    assert calls == 1


async def test_disable_cancels_pending_registration(monkeypatch):
    unregistered = asyncio.Event()

    async def unregister(_nc):
        unregistered.set()

    monkeypatch.setattr(integrations, "INTEGRATION_RETRY_DELAYS", (60,))
    monkeypatch.setattr(integrations, "_unregister_integrations", unregister)

    nc = FakeNextcloud()
    await integrations.enabled_handler(True, nc)
    task = integrations._integration_task
    assert task is not None

    assert await integrations.enabled_handler(False, nc) == ""
    assert task.cancelled()
    assert unregistered.is_set()
