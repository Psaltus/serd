"""Application logging: levels, structured output, and CloudWatch delivery.

Two things read these logs, and they want different shapes:

* a person tailing `docker compose logs`, who wants one line per event;
* CloudWatch Logs Insights, which can filter and aggregate on fields but only
  if the line is JSON.

JSON wins, because a person can still read it and the alternative cannot be
queried. Every line carries the level, the logger, and - inside a request - the
id that ties it to the nginx access log entry for the same request.

Delivery is deliberately belt-and-braces. Everything goes to stdout, which is
what a container log driver (the ECS awslogs driver, a CloudWatch agent, or
`docker logs`) collects. A handler that puts events into CloudWatch directly
can be switched on as well, for somewhere that has no such driver. Logging must
never be the reason the app is down, so failures to attach that handler are
logged and swallowed.
"""

import logging
import json
import socket
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

import boto3

from .config import settings

logger = logging.getLogger(__name__)

# Set per request by the middleware in main.py, so any log line emitted while
# handling a request can be tied back to it without threading an argument
# through every function.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# Set by the same middleware while a health check is being served, so that
# everything logged under it can be recognised - not just the request line.
health_check_var: ContextVar[bool] = ContextVar("health_check", default=False)

# Paths whose traffic is not worth paying CloudWatch to store.
HEALTH_PATHS = frozenset({"/api/healthz"})

VALID_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})

# Attributes every LogRecord carries. Anything a caller attaches through
# `extra=` will not be in here, which is how those fields are picked out and
# merged into the JSON payload.
_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
        # uvicorn attaches an ANSI-coloured copy of its own messages. It is
        # already in `message` without the escape codes.
        "color_message",
    }
)

# Handlers are module state so the level can be changed, and the CloudWatch
# handler flushed, after start-up.
_stream_handler: Optional[logging.Handler] = None
_cloudwatch_handler: Optional[logging.Handler] = None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Whatever the caller passed as extra={...}.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str so an unexpected object in `extra` cannot raise inside
        # logging and take a request down with it.
        return json.dumps(payload, default=str)


class RequestIdFilter(logging.Filter):
    """Stamps the current request's id onto every record emitted under it."""

    def filter(self, record: logging.LogRecord) -> bool:
        request_id = request_id_var.get()
        if request_id:
            record.request_id = request_id
        return True


class ExcludeHealthChecks(logging.Filter):
    """Drops health-check traffic on its way to CloudWatch.

    A liveness probe runs every few seconds for the life of the service. Stored
    at a line per probe that is a standing ingestion and retention charge for
    the least interesting traffic there is, and it buries real events. This is
    attached only to the CloudWatch handler, so the lines still reach stdout,
    where they cost nothing and are useful when watching a container locally.

    The context variable rather than the path alone: anything logged while
    serving a probe should be dropped too, not just the request line.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "path", None) in HEALTH_PATHS:
            return False
        return not health_check_var.get()


def effective_level() -> int:
    """The level to log at, and the one place `debug` is interpreted.

    The debug flag is a deliberate override rather than just another level, so
    turning it on cannot be undone by whatever log_level happens to be set.
    """
    if settings.debug:
        return logging.DEBUG
    name = (settings.log_level or "INFO").upper()
    if name not in VALID_LEVELS:
        # A typo in a parameter should not silence the logs or stop the app.
        logging.getLogger(__name__).warning(
            "Unknown log level %r; falling back to INFO", settings.log_level
        )
        return logging.INFO
    return getattr(logging, name)


def configure() -> None:
    """Install the stdout handler. Safe to call once, early, before config."""
    global _stream_handler

    _stream_handler = logging.StreamHandler(sys.stdout)
    _stream_handler.setFormatter(JsonFormatter())
    _stream_handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(_stream_handler)
    root.setLevel(effective_level())

    # uvicorn ships its own handlers and would otherwise print a second,
    # unstructured copy of everything.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        log = logging.getLogger(name)
        log.handlers.clear()
        log.propagate = True
    # Our middleware logs every request with more detail than uvicorn's access
    # line, so silence the duplicate rather than emitting both.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # botocore logs the whole of every request and response at DEBUG. Useful
    # occasionally, overwhelming always, so it is held one level higher than
    # the app when debugging is on.
    logging.getLogger("botocore").setLevel(logging.INFO)
    logging.getLogger("boto3").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.INFO)


def apply_level() -> str:
    """Re-apply the configured level. Returns the level name now in force."""
    level = effective_level()
    logging.getLogger().setLevel(level)
    return logging.getLevelName(level)


def attach_cloudwatch() -> bool:
    """Send log records to CloudWatch Logs directly. Returns whether it started.

    Not needed where a container log driver already forwards stdout - turning
    it on there just pays for the same line twice. It exists for anywhere that
    has no such driver, and to make the CloudWatch path testable locally.
    """
    global _cloudwatch_handler

    if not settings.cloudwatch_logs_enabled:
        return False
    log_group = settings.log_group
    if not settings.platform_env and not settings.cloudwatch_log_group:
        logger.warning(
            "CloudWatch logging is enabled but the platform environment is "
            "unset, so no log group can be derived; logging to stdout only"
        )
        return False
    if _cloudwatch_handler is not None:
        return True

    try:
        import watchtower

        client = boto3.client("logs", **settings.aws_client_kwargs)
        handler = watchtower.CloudWatchLogHandler(
            log_group_name=log_group,
            # One stream per container, so concurrent tasks do not interleave.
            log_stream_name=settings.cloudwatch_log_stream or socket.gethostname(),
            boto3_client=client,
            create_log_group=True,
            # Batched on a background thread; this only bounds how long a line
            # can sit unsent, it does not block the request that produced it.
            send_interval=5,
        )
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RequestIdFilter())
        # Only on this handler: stdout keeps the health checks, CloudWatch is
        # not billed for them.
        handler.addFilter(ExcludeHealthChecks())
        logging.getLogger().addHandler(handler)
        _cloudwatch_handler = handler
    except Exception:
        # Missing permissions, an unreachable endpoint, a missing dependency -
        # none of it is worth failing to start over, because stdout still works.
        logger.exception(
            "Could not start CloudWatch logging; continuing with stdout only"
        )
        return False

    logger.info(
        "CloudWatch logging started",
        extra={
            "log_group": log_group,
            "log_stream": settings.cloudwatch_log_stream or socket.gethostname(),
        },
    )
    return True


def shutdown() -> None:
    """Flush anything the CloudWatch handler is still holding."""
    if _cloudwatch_handler is not None:
        try:
            _cloudwatch_handler.close()
        except Exception:  # pragma: no cover - shutdown must not raise
            logger.debug("Flushing CloudWatch handler failed", exc_info=True)
