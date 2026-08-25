from .campaigns import router as campaigns_router
from .dashboard import router as dashboard_router
from .events import router as events_router
from .imports import router as imports_router
from .organizations import router as organizations_router
from .reports import router as reports_router

ALL_ROUTERS = (
    dashboard_router,
    campaigns_router,
    organizations_router,
    imports_router,
    reports_router,
    events_router,
)
