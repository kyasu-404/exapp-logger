from __future__ import annotations

import logging
from contextlib import suppress

from nc_py_api import AsyncNextcloudApp

from .security import webhook_token
from .services.events import FULL_EVENT_CLASSES

LOGGER = logging.getLogger(__name__)
MENU_NAME = "file_control"
EVENT_HANDLER = "/api/events/nextcloud"


async def _register_current_webhooks(nc: AsyncNextcloudApp) -> None:
    await nc.webhooks.unregister_all()
    headers = {"X-File-Control-Webhook": webhook_token()}
    for event_class in FULL_EVENT_CLASSES.values():
        await nc.webhooks.register("POST", EVENT_HANDLER, event_class, headers=headers)


async def _unregister_integrations(nc: AsyncNextcloudApp) -> None:
    for action in (
        lambda: nc.ui.resources.delete_script("top_menu", MENU_NAME, "js/app.js"),
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
    if not enabled:
        await _unregister_integrations(nc)
        return ""
    try:
        await nc.ui.top_menu.register(MENU_NAME, "Сбор файлов", "img/app.svg")
        await nc.ui.resources.set_script("top_menu", MENU_NAME, "js/app.js")
        await nc.ui.resources.set_style("top_menu", MENU_NAME, "css/app.css")
        try:
            await _register_current_webhooks(nc)
            LOGGER.info("Зарегистрированы Webhook Listeners для файловых событий")
        except Exception as webhook_exc:
            return (
                "Не удалось зарегистрировать события файлов. Включите системное приложение "
                f"webhook_listeners и повторите включение ExApp. Ошибка: {webhook_exc}"
            )
        return ""
    except Exception as exc:
        LOGGER.exception("Ошибка регистрации интеграций Nextcloud")
        return f"Не удалось подключить интерфейс ExApp: {exc}"
