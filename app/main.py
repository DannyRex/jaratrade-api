"""FastAPI app entrypoint.

Auto-creates tables and seeds reference data on startup so the API is usable
out-of-the-box with `uvicorn app.main:app --reload`.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .database import Base, SessionLocal, engine
from .envelope import success
from . import observability  # noqa: F401 - initialises Sentry + OTel at import
from .seed import seed_default_data

settings = get_settings()


def _apply_migrations() -> None:
    """Run Alembic upgrade to head. Falls back to create_all if alembic isn't set up
    (e.g. test envs that point at an in-memory DB).
    """
    try:
        from pathlib import Path

        from alembic import command
        from alembic.config import Config

        ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
        if not ini_path.exists():
            raise FileNotFoundError(ini_path)

        cfg = Config(str(ini_path))
        cfg.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(cfg, "head")
    except Exception as e:
        # In dev/test the simpler path is fine.
        print(f"[migrations] alembic upgrade failed ({e!r}); falling back to create_all")
        Base.metadata.create_all(bind=engine)


async def _log_outbound_ip() -> None:
    """Log the server's outbound IP on startup.

    Flutterwave's Transfers API (used for seller payouts) only accepts
    requests from whitelisted IPs, so the egress IP must be added to the
    Flutterwave dashboard. Printing it here keeps the value to whitelist
    visible in every deploy's logs.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get("https://api.ipify.org")
        print(f"[network] outbound IP (whitelist this with Flutterwave): {resp.text.strip()}")
    except Exception as e:  # noqa: BLE001
        print(f"[network] could not determine outbound IP: {e!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _apply_migrations()
    with SessionLocal() as db:
        try:
            seed_default_data(db)
        except Exception as e:
            # When uvicorn runs with multiple workers, both will race to seed.
            # The losing worker hits a UniqueViolation - fine, the data's already
            # there from the winning worker. Log and move on.
            db.rollback()
            print(f"[seed] skipped ({e.__class__.__name__}); another worker probably won the race")
    # Real deploys only (Postgres) - skip the network probe under tests/sqlite.
    if settings.database_url.startswith("postgres"):
        await _log_outbound_ip()
    yield


app = FastAPI(
    title=settings.app_name,
    description="Jaratrade marketplace API - Nigeria↔UK B2B trade.",
    version="2.0.0",
    lifespan=lifespan,
)

# Attach OTel auto-instrumentations now (before any middleware) so request
# spans are emitted from request 1.
observability.instrument_app(app, engine)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────── Rate limiting (slowapi) ─────────────────────
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402

from .rate_limit import limiter  # noqa: E402

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ───────────────────── Request logging + IDs ─────────────────────
import logging  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402

logger = logging.getLogger("jaratrade.access")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


@app.middleware("http")
async def request_id_and_logging(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    start = time.perf_counter()
    request.state.request_id = rid
    # Tag the OTel span + Sentry scope with our request ID for correlation.
    observability.annotate_request(rid)
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.exception("rid=%s %s %s ERROR %.1fms", rid, request.method, request.url.path, elapsed_ms)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = rid
    logger.info(
        "rid=%s %s %s %s %.1fms",
        rid,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


# ───────────────────────── Exception → enveloped JSON ─────────────────────────

@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "status" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": False, "message": str(detail), "errors": [str(detail)]},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query", "path"))
        msg = err.get("msg", "Invalid value")
        errors.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"status": False, "message": "Validation failed", "errors": errors},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled 500s.

    Starlette's ServerErrorMiddleware sits OUTSIDE CORSMiddleware, so a raw
    unhandled exception reaches the browser with no Access-Control-Allow-Origin
    header - the browser then reports an opaque "Failed to fetch" and the
    real error is invisible to the client. Echoing the CORS headers here
    means a 500 surfaces as an actual 500 the frontend can show + log.
    """
    import traceback
    traceback.print_exc()

    headers: dict[str, str] = {}
    origin = request.headers.get("origin")
    allowed = settings.cors_origins
    if origin and ("*" in allowed or origin in allowed):
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
    return JSONResponse(
        status_code=500,
        content={
            "status": False,
            "message": "Something went wrong on our end. Please try again.",
            "errors": ["Internal server error"],
        },
        headers=headers,
    )


# ───────────────────────── Health & root ─────────────────────────

@app.get("/")
def root():
    return success({"name": settings.app_name, "version": "2.0.0", "docs": "/docs"})


@app.get("/health")
def health():
    return success({"healthy": True})


# ───────────────────────── Routers ─────────────────────────
# Imports kept inside main to avoid circular import overhead during model setup.
from .routers import (  # noqa: E402
    public,
    auth,
    importer,
    exporter,
    admin,
    admin_users,
    subscriptions,
    disputes,
    settings_router,
    bank_router,
    logs,
    payouts,
    flw_webhook,
)

app.include_router(public.router)
app.include_router(auth.router)
app.include_router(importer.router)
app.include_router(exporter.router)
app.include_router(admin.router)
app.include_router(admin_users.router)
app.include_router(subscriptions.importer_router)
app.include_router(subscriptions.exporter_router)
app.include_router(disputes.importer_router)
app.include_router(disputes.exporter_router)
app.include_router(disputes.admin_router)
app.include_router(settings_router.router)
app.include_router(bank_router.router)
app.include_router(logs.router)
app.include_router(payouts.router)
app.include_router(flw_webhook.router)
