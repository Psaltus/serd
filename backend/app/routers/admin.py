"""Operational endpoints: rebuilding the index, logging control, health."""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from .. import logging_setup, parameters, storage
from ..cache import cache
from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class RefreshResponse(BaseModel):
    status: str


class LoggingResponse(BaseModel):
    status: str
    log_level: str
    debug: bool
    reloaded: dict[str, str]


@router.post("/refresh", response_model=RefreshResponse)
def refresh() -> RefreshResponse:
    """Rebuild the index from the bucket.

    This is how a file added to S3 by any route other than this app - the
    console, the CLI, another service - becomes downloadable without waiting
    for anything or restarting the container. It also clears remembered
    misses, so a name that 404'd before the file was put there stops 404ing.

    Returns nothing about the bucket's contents - not the names, and not even
    a count. The number indexed goes to the log, where an operator can see it
    and a caller cannot.
    """
    count = storage.rebuild_cache()
    logger.info(
        "File count in cache: %s", count,
    )
    return RefreshResponse(status="ok")


@router.post("/logging", response_model=LoggingResponse)
def reload_logging() -> LoggingResponse:
    """Re-read the debug flag and log level from Parameter Store, and apply them.

    Debugging is switched on precisely when something is already wrong, and
    restarting a container to raise its verbosity tends to destroy the state
    worth looking at. So this one slice of configuration can be re-read while
    the process is serving; everything else stays start-up only.

    Change the parameter, then call this:
        aws ssm put-parameter --name <path>/debug --value true --overwrite
        curl -X POST http://<host>/api/logging
    """
    reloaded = parameters.reload_runtime_parameters()
    level = logging_setup.apply_level()
    logger.info(
        "Log level now %s (debug=%s)", level, settings.debug,
        extra={"log_level": level, "debug": settings.debug},
    )
    return LoggingResponse(
        status="ok", log_level=level, debug=settings.debug, reloaded=reloaded
    )


@router.get("/logging", response_model=LoggingResponse)
def current_logging() -> LoggingResponse:
    """What this container is logging at right now, without changing anything."""
    return LoggingResponse(
        status="ok",
        log_level=logging.getLevelName(logging.getLogger().level),
        debug=settings.debug,
        reloaded={},
    )


@router.get("/healthz")
def healthz() -> dict:
    """Liveness only.

    Deliberately does not touch S3: a health check that fails when the bucket
    is briefly unreachable would take down containers that are still able to
    serve everything already indexed.
    """
    return {"status": "ok"}
