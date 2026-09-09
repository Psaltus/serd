import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import logging_setup, storage
from .config import settings
from .parameters import load_parameters
from .routers import admin, files, pages
from .views import render_error

# Logging is set up before anything else so that configuration loading, which
# is the most likely thing to fail at start-up, is itself logged. The level at
# this point comes from the environment; once parameters are in it is applied
# again below.
logging_setup.configure()
logger = logging.getLogger(__name__)

# Worth being loud about: every AWS call below - Parameter Store, S3 and
# CloudWatch alike - is going somewhere that is not AWS. Silently running
# against an emulator is the kind of thing that wastes an afternoon.
if settings.use_local_stack:
    logger.warning(
        "SERDSAFE_LOCAL_STACK is set: all AWS calls go to %s, not real AWS",
        settings.aws_endpoint_url,
        extra={"aws_endpoint": settings.aws_endpoint_url},
    )

# Configuration is applied at import rather than in the lifespan below, so
# anything constructed while this module is read already sees the final values.
# A failure raises and the process exits, which is the intent — see
# parameters.load_parameters.
load_parameters()

# Now that log_level and debug are known, re-apply them and start CloudWatch
# delivery if it is switched on. Lines logged before this point went to stdout
# only, which is where a container log driver collects them anyway.
_level = logging_setup.apply_level()
logging_setup.attach_cloudwatch()
logger.info("Logging at %s", _level, extra={"log_level": _level})


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the index once, up front, so the first request costs nothing. Not
    # fatal on failure: the pages and the refresh endpoint still work with an
    # empty index, and rebuild_cache keeps whatever it had.
    count = storage.rebuild_cache()
    logger.info("Ready with %d file(s) indexed", count, extra={"files": count})
    try:
        yield
    finally:
        # Anything the CloudWatch handler has batched but not yet sent would
        # otherwise be lost when the process exits.
        logging_setup.shutdown()


# The interactive docs are disabled so the API surface cannot be enumerated.
app = FastAPI(title="SerdSafely", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One log line per request, and an id that ties it to the nginx entry.

    nginx sends its own $request_id, so a single identifier follows a request
    across both logs. One is generated here if the app is reached directly.
    """
    request_id = request.headers.get("x-request-id") or uuid4().hex
    token = logging_setup.request_id_var.set(request_id)
    # Marks the whole request, so the CloudWatch filter can drop everything
    # logged while serving a probe rather than only the request line.
    health_token = logging_setup.health_check_var.set(
        request.url.path in logging_setup.HEALTH_PATHS
    )
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        # The exception handlers below turn this into a response; log it here
        # too so the request line is never missing for a failed request.
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception(
            "%s %s failed", request.method, request.url.path,
            extra={
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
            },
        )
        logging_setup.request_id_var.reset(token)
        logging_setup.health_check_var.reset(health_token)
        raise

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    # Health checks are frequent and say nothing when they pass, so they are
    # kept at DEBUG rather than filling the log.
    level = logging.DEBUG if request.url.path == "/api/healthz" else logging.INFO
    if response.status_code >= 500:
        level = logging.ERROR
    elif response.status_code >= 400:
        level = logging.WARNING

    logger.log(
        level,
        "%s %s %s",
        request.method,
        request.url.path,
        response.status_code,
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "client": request.client.host if request.client else None,
        },
    )

    response.headers["X-Request-Id"] = request_id
    logging_setup.request_id_var.reset(token)
    logging_setup.health_check_var.reset(health_token)
    return response


# --- Errors ----------------------------------------------------------------
# Every failure leaves through render_error, so a browser gets the same styled
# page nginx serves for its own errors, and an API caller gets JSON. Without
# these, FastAPI's defaults would return bare JSON to browsers and Starlette's
# unhandled-exception path would return plain text.


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    # 4xx is the caller's business and already appears on the request line, so
    # it is only worth a line of its own at DEBUG.
    logger.log(
        logging.WARNING if exc.status_code >= 500 else logging.DEBUG,
        "%s on %s: %s", exc.status_code, request.url.path, exc.detail,
        extra={"status": exc.status_code, "path": request.url.path},
    )
    return render_error(request, exc.status_code, detail=exc.detail)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> Response:
    logger.debug(
        "Invalid request to %s: %s", request.url.path, exc.errors(),
        extra={"path": request.url.path},
    )
    return render_error(request, 400, detail="Invalid request")


@app.exception_handler(ClientError)
async def handle_client_error(request: Request, exc: ClientError) -> Response:
    code = exc.response.get("Error", {}).get("Code", "Unknown")
    logger.warning(
        "AWS error %s on %s: %s", code, request.url.path, exc,
        extra={"aws_error_code": code, "path": request.url.path},
    )
    return render_error(request, 502)


@app.exception_handler(BotoCoreError)
async def handle_botocore_error(request: Request, exc: BotoCoreError) -> Response:
    # Missing credentials, endpoint resolution, connection failures.
    logger.warning(
        "AWS transport error on %s: %s", request.url.path, exc,
        extra={"path": request.url.path},
    )
    return render_error(request, 502)


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> Response:
    logger.exception(
        "Unhandled error on %s %s", request.method, request.url.path,
        extra={"method": request.method, "path": request.url.path},
    )
    return render_error(request, 500)


app.include_router(pages.router)
app.include_router(files.router)
app.include_router(admin.router)
