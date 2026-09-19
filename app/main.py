"""FastAPI application entry point."""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.errors import AppError
from app.clients import db


def configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout,
                        level=getattr(logging, settings.log_level.upper(), logging.INFO))
    # httpx logs every request line at INFO — full URL, query string included. Any client
    # that authenticates by query parameter puts its secret in the log that way. Belt and
    # braces with the header change in clients/embed.py.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("startup", version=settings.version, model=settings.rkapi_model,
             lanes=len(settings.rkapi_keys))
    try:
        await db.init_pool()
    except Exception as e:  # noqa: BLE001 — the API still serves /healthz without a DB
        log.error("startup.db_failed", error=str(e)[:200])
    yield
    await db.close_pool()
    log.info("shutdown")


app = FastAPI(
    title="Anker Care Agent",
    version=settings.version,
    description="After-sales support agent — Anker Hackathon Track 4",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Event-Vocab"],
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled", path=str(request.url.path))
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL", "message": str(exc)[:300], "detail": {}}},
    )


from app.routes import catalog, chat, health, tickets, attachments, evaluation  # noqa: E402

app.include_router(health.router)
app.include_router(chat.router, prefix="/api/v1")
app.include_router(catalog.router, prefix="/api/v1")
app.include_router(tickets.router, prefix="/api/v1")
app.include_router(attachments.router, prefix="/api/v1")
app.include_router(evaluation.router, prefix="/api/v1")
