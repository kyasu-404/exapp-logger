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
from .services.reconciliation import reconcile_all_active

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
            await reconcile_all_active()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Фоновая сверка завершилась с ошибкой")
        await asyncio.sleep(settings.reconcile_interval_minutes * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_handlers(app, enabled_handler)
    await init_database()
    task = asyncio.create_task(reconciliation_loop()) if in_nextcloud_runtime() else None
    LOGGER.info("Приложение запущено; хранилище=%s", settings.storage_path)
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await close_database()


APP = FastAPI(
    title="Сбор и контроль файлов",
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
