from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from nc_py_api.ex_app import AppAPIAuthMiddleware, run_app, set_handlers

from .config import get_settings, in_nextcloud_runtime
from .db import close_database, init_database
from .integrations import enabled_handler
from .routers import ALL_ROUTERS
from .services.folder_scope import synchronize_active_watch_scopes
from .services.reconciliation import reconcile_all_active
from .services.scheduled_reports import run_due_reports

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger(__name__)


async def reconciliation_loop() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            changed = await synchronize_active_watch_scopes()
            if changed:
                LOGGER.info("Автоматически уточнён тип папки у %s наблюдений", changed)
            await reconcile_all_active()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Фоновая сверка завершилась с ошибкой")
        await asyncio.sleep(settings.reconcile_interval_minutes * 60)


async def scheduled_reports_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await run_due_reports()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Проверка расписаний отчётов завершилась с ошибкой")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_handlers(app, enabled_handler)
    await init_database()
    tasks = (
        [asyncio.create_task(reconciliation_loop()), asyncio.create_task(scheduled_reports_loop())]
        if in_nextcloud_runtime()
        else []
    )
    LOGGER.info("Приложение запущено; хранилище=%s", settings.storage_path)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await close_database()


APP = FastAPI(
    title="Отчёт сдачи",
    version=settings.app_version,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
if in_nextcloud_runtime():
    APP.add_middleware(AppAPIAuthMiddleware)

for api_router in ALL_ROUTERS:
    APP.include_router(api_router)


@APP.exception_handler(Exception)
async def unhandled_exception(_request, exc: Exception):
    LOGGER.exception("Необработанная ошибка API", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка приложения"})


def main() -> None:
    os.environ.setdefault("APP_PORT", "23000")
    run_app("file_control.main:APP", log_level=settings.log_level.lower(), access_log=settings.debug)


if __name__ == "__main__":
    main()
