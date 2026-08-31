from .dashboard import router as dashboard_router
from .events import router as events_router
from .reports import router as reports_router
from .watches import router as watches_router

ALL_ROUTERS = (
    dashboard_router,
    watches_router,
    reports_router,
    events_router,
)
