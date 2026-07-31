from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes_category_performance import router as category_performance_router
from app.api.routes_category_scores import router as category_scores_router
from app.api.routes_contributions import router as contributions_router
from app.api.routes_health import router as health_router
from app.api.routes_nodes import router as nodes_router
from app.api.routes_projects import router as projects_router
from app.api.routes_reputation import router as reputation_router
from app.api.routes_rewards import router as rewards_router
from app.api.routes_reports import router as reports_router
from app.api.routes_reproduction import router as reproduction_router
from app.api.routes_routing import router as routing_router
from app.api.routes_subnet_rewards import router as subnet_rewards_router
from app.api.routes_submissions import router as submissions_router
from app.api.routes_subnets import router as subnets_router
from app.api.routes_subnet_membership import router as subnet_membership_router
from app.api.routes_validation import router as validation_router
from app.core.database import init_db
from app.core.openapi import API_DESCRIPTION, OPENAPI_TAGS, configure_openapi


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    yield


app = FastAPI(
    title="ProofGuard Audit API",
    description=API_DESCRIPTION,
    version="0.1.0",
    openapi_tags=OPENAPI_TAGS,
    docs_url="/docs",
    redoc_url="/redoc",
    swagger_ui_parameters={
        "defaultModelsExpandDepth": 1,
        "displayRequestDuration": True,
        "filter": True,
    },
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(health_router)
app.include_router(nodes_router)
app.include_router(subnets_router)
app.include_router(subnet_membership_router)
app.include_router(category_performance_router)
app.include_router(category_scores_router)
app.include_router(submissions_router)
app.include_router(contributions_router)
app.include_router(reputation_router)
app.include_router(rewards_router)
app.include_router(projects_router)
app.include_router(routing_router)
app.include_router(subnet_rewards_router)
app.include_router(reproduction_router)
app.include_router(validation_router)
app.include_router(reports_router)

configure_openapi(app)
