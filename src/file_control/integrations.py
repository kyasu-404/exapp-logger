from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from nc_py_api import AsyncNextcloudApp

from .security import webhook_token
from .services.events import FULL_EVENT_CLASSES

LOGGER = logging.getLogger(__name__)
MENU_NAME = "file_control"
EVENT_HANDLER = "/api/events/nextcloud"
INTEGRATION_RETRY_DELAYS = (1, 2, 4, 8, 15, 30)

_integration_task: asyncio.Task[None] | None = None


async def _register_current_webhooks(nc: AsyncNextcloudApp) -> None:
    # Nextcloud returns 404 here when no listener exists (and older
    # webhook_listeners versions do the same for the bulk-delete route).
    # Registration below is still valid in both cases.
    with suppress(Exception):
        await nc.webhooks.unregister_all()
    headers = {"X-File-Control-Webhook": webhook_token()}
    for event_class in FULL_EVENT_CLASSES.values():
        await nc.webhooks.register("POST", EVENT_HANDLER, event_class, headers=headers)


async def _register_integrations(nc: AsyncNextcloudApp) -> None:
    # AppAPI appends .js/.css itself. Clean up the 1.0.1 entries that already
    # included the extension and consequently produced app.js.js/app.css.css.
    with suppress(Exception):
        await nc.ui.resources.delete_script("top_menu", MENU_NAME, "js/app.js")
    with suppress(Exception):
        await nc.ui.resources.delete_style("top_menu", MENU_NAME, "css/app.css")
    await nc.ui.top_menu.register(MENU_NAME, "Отчёт сдачи", "img/app.svg")
    await nc.ui.resources.set_script("top_menu", MENU_NAME, "js/app")
    await nc.ui.resources.set_style("top_menu", MENU_NAME, "css/app")
    await _register_current_webhooks(nc)


async def _register_integrations_after_enable(nc: AsyncNextcloudApp) -> None:
    """Register UI after AppAPI has published the new enabled state.

    Some AppAPI/Redis combinations briefly expose the previous cached state to
    requests made from inside the ``/enabled`` callback. Returning from that
    callback first avoids a false 401 and the resulting enable rollback.
    """
    last_error: Exception | None = None
    for attempt, delay in enumerate(INTEGRATION_RETRY_DELAYS, start=1):
        await asyncio.sleep(delay)
        try:
            if not await nc.enabled_state:
                raise RuntimeError("AppAPI ещё не опубликовал состояние enabled")
            await _register_integrations(nc)
            LOGGER.info("Интерфейс и Webhook Listeners зарегистрированы")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            LOGGER.warning(
                "Регистрация интеграций, попытка %s/%s: %s",
                attempt,
                len(INTEGRATION_RETRY_DELAYS),
                exc,
            )
    LOGGER.error("Не удалось зарегистрировать интеграции после включения: %s", last_error)


def _clear_integration_task(task: asyncio.Task[None]) -> None:
    global _integration_task
    if _integration_task is task:
        _integration_task = None


def _schedule_integration_registration(nc: AsyncNextcloudApp) -> None:
    global _integration_task
    if _integration_task is not None and not _integration_task.done():
        return
    _integration_task = asyncio.create_task(_register_integrations_after_enable(nc))
    _integration_task.add_done_callback(_clear_integration_task)


async def _unregister_integrations(nc: AsyncNextcloudApp) -> None:
    for action in (
        lambda: nc.ui.resources.delete_script("top_menu", MENU_NAME, "js/app"),
        lambda: nc.ui.resources.delete_script("top_menu", MENU_NAME, "js/app.js"),
        lambda: nc.ui.resources.delete_style("top_menu", MENU_NAME, "css/app"),
        lambda: nc.ui.resources.delete_style("top_menu", MENU_NAME, "css/app.css"),
        lambda: nc.ui.top_menu.unregister(MENU_NAME),
        lambda: nc.webhooks.unregister_all(),
    ):
        try:
            await action()
        except Exception as exc:
            LOGGER.debug("Интеграция уже отсутствует или недоступна: %s", exc)
    with suppress(Exception):
        await nc._session.ocs(  # noqa: SLF001
            "DELETE",
            f"{nc._session.ae_url}/events_listener",  # noqa: SLF001
            json={"eventType": "node_event"},
        )


async def enabled_handler(enabled: bool, nc: AsyncNextcloudApp) -> str:
    global _integration_task
    if not enabled:
        if _integration_task is not None and not _integration_task.done():
            _integration_task.cancel()
            with suppress(asyncio.CancelledError):
                await _integration_task
        _integration_task = None
        await _unregister_integrations(nc)
        return ""
    _schedule_integration_registration(nc)
    return ""
