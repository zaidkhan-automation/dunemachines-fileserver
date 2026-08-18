"""
Dunemachines File Server v1.0.0
Asset-centric file management with AI capabilities.
Port: 8007
"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.events import on_startup, on_shutdown
from app.core.rate_limit import limiter
from app.api.rest.uploads import router as uploads_router
from app.api.rest.files import router as assets_router
from app.api.rest.folders import router as folders_router
from app.api.rest.projects import router as projects_router
from app.api.rest.auth import router as auth_router
from app.api.rest.connectors import router as connectors_router
from app.api.rest.permissions import router as permissions_router
from app.api.rest.presentation_links import router as presentation_links_router, public_router as presentation_links_public_router
from app.connectors.github.webhook_handler import router as github_webhook_router
from app.api.graphql.schema import graphql_router
from app.api.ws.collaboration import router as ws_router, manager as ws_manager

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await on_startup()
    yield
    await on_shutdown()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    description="Asset-centric file management platform with AI capabilities",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Middleware ────────────────────────────────────────────────────
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Error Handlers ────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # exc.errors() can embed a raw exception object in ctx.error for
    # field_validator-raised errors (unlike plain Field constraints, whose
    # ctx only holds plain values) — JSONResponse can't json.dumps that
    # directly, so it must go through jsonable_encoder first.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "detail": jsonable_encoder(exc.errors()),
            "body": str(exc.body)[:500] if exc.body else None,
        },
    )


@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error on {request.method} {request.url}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_server_error", "detail": str(exc) if settings.DEBUG else "An error occurred"},
    )


# ── Routes ────────────────────────────────────────────────────────
PREFIX = "/api/v1"

app.include_router(auth_router,     prefix=PREFIX)
app.include_router(uploads_router,  prefix=PREFIX)
app.include_router(assets_router,   prefix=PREFIX)
app.include_router(folders_router,  prefix=PREFIX)
app.include_router(projects_router, prefix=PREFIX)
app.include_router(connectors_router, prefix=PREFIX)
app.include_router(permissions_router, prefix=PREFIX)
app.include_router(presentation_links_router, prefix=PREFIX)
app.include_router(presentation_links_public_router, prefix=PREFIX)
app.include_router(github_webhook_router, prefix="/api/v1")
app.include_router(graphql_router, prefix="/graphql")
app.include_router(ws_router)


# ── Health & Info ─────────────────────────────────────────────────
@app.get("/health", tags=["system"])
async def health():
    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "version": settings.VERSION,
    }


@app.get("/info", tags=["system"])
async def info():
    return {
        "service": settings.APP_NAME,
        "version": settings.VERSION,
        "endpoints": {
            "auth":     f"{PREFIX}/auth",
            "uploads":  f"{PREFIX}/uploads",
            "assets":   f"{PREFIX}/assets",
            "folders":  f"{PREFIX}/folders",
            "projects": f"{PREFIX}/projects",
            "graphql":  "/graphql",
            "ws":       "/ws",
        },
        "ws_connections": ws_manager.total_connections(),
    }


if __name__ == "__main__":
    import uvicorn
    from app.core.logging_filters import RedactTokenFilter

    log_config = uvicorn.config.LOGGING_CONFIG
    log_config.setdefault("filters", {})["redact_ws_token"] = {
        "()": RedactTokenFilter
    }
    log_config["loggers"]["uvicorn.error"].setdefault("filters", []).append(
        "redact_ws_token"
    )

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=settings.DEBUG,
        workers=1 if settings.DEBUG else 2,
        log_level="debug" if settings.DEBUG else "info",
        log_config=log_config,
    )
