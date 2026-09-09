"""Deployment configuration read from SSM Parameter Store at start-up.

Which bucket this container serves is a property of the deployment, not of the
image. One environment variable, SERDSAFE_PLATFORM_ENV, names the environment;
everything else is read from `/<env>/...` in Parameter Store. So the same image
runs in dev, staging and production, and pointing an environment somewhere new
is a parameter edit and a restart rather than a rebuild.

Read once, during start-up, and never again except for the logging settings -
see reload_runtime_parameters. Re-reading which bucket to serve while the
process is live would let long-running containers drift onto different
configuration from one another, with no deploy to explain it.
"""

import logging
import re
from typing import Optional

import boto3
from pydantic import ValidationError

from .config import settings

logger = logging.getLogger(__name__)

# Only these are applied, matched on the last segment of the parameter name.
# Anything else under the path is ignored rather than applied, so a parameter
# filed alongside them cannot reconfigure the app. aws_region and the platform
# environment itself are absent: both are needed to reach Parameter Store.
ALLOWED_KEYS = frozenset(
    {
        "s3_bucket",
        "s3_prefix",
        "negative_cache_seconds",
        "log_level",
        "debug",
        "cloudwatch_logs_enabled",
        "cloudwatch_log_group",
        "cloudwatch_log_stream",
    }
)

# The subset that may be re-read on a running container. Verbosity is the one
# thing worth changing without a restart: debugging is turned on because
# something is already wrong, and restarting to enable it often destroys the
# state being investigated. Which bucket to serve stays start-up only.
RUNTIME_KEYS = frozenset({"log_level", "debug"})

# Environment names go straight into a Parameter Store path and a log group
# name, so they are kept to something that cannot change the meaning of either.
VALID_ENV = re.compile(r"[A-Za-z0-9._-]+")


def require_platform_env() -> str:
    """The validated environment name, or a clear reason why we cannot start."""
    env = settings.platform_env
    if not env:
        raise RuntimeError(
            "SERDSAFE_PLATFORM_ENV is not set. It names the Parameter Store path "
            "this container reads its configuration from (/<env>/s3_bucket) and "
            "its CloudWatch log group, so there is no safe default to fall back "
            "on."
        )
    if not VALID_ENV.fullmatch(env):
        raise RuntimeError(
            f"SERDSAFE_PLATFORM_ENV={env!r} is not usable as a path segment. "
            "Use letters, digits, dots, dashes or underscores."
        )
    return env


def _fetch(path: str) -> dict[str, str]:
    """Read the parameters directly under `path`, keyed by their last segment.

    Deliberately not recursive. The layout is flat - /<env>/s3_bucket - and a
    recursive read of an environment's whole subtree would pull in, and decrypt,
    unrelated parameters that happen to live beneath it.
    """
    client = boto3.client("ssm", **settings.aws_client_kwargs)
    values: dict[str, str] = {}
    for page in client.get_paginator("get_parameters_by_path").paginate(
        Path=path, Recursive=False, WithDecryption=True
    ):
        for param in page.get("Parameters", []):
            values[param["Name"].rsplit("/", 1)[-1].lower()] = param["Value"]
    return values


def load_parameters() -> list[str]:
    """Apply this environment's parameters to `settings`. Returns what was applied.

    Failure is deliberately fatal. Carrying on with whatever the environment
    happened to supply would start a container that looks healthy while serving
    an empty bucket, or another environment's. Exiting instead surfaces the
    mistake at deploy time, while a scheduler can still roll the release back.
    """
    require_platform_env()
    path = settings.param_path

    values = _fetch(path)
    if not values:
        raise RuntimeError(
            f"No parameters found under {path}. Check SERDSAFE_PLATFORM_ENV, and "
            "that the task role allows ssm:GetParametersByPath on that path."
        )

    applied: list[str] = []
    for name, value in sorted(values.items()):
        if name not in ALLOWED_KEYS:
            logger.warning("Ignoring unrecognised parameter %s/%s", path, name)
            continue
        try:
            setattr(settings, name, value)
        except ValidationError as exc:
            raise RuntimeError(f"Parameter {path}/{name} is not usable: {exc}") from exc
        applied.append(name)

    if not applied:
        raise RuntimeError(
            f"Parameters exist under {path} but none are recognised. Expected one "
            f"or more of: {', '.join(sorted(ALLOWED_KEYS))}."
        )
    if not settings.s3_bucket:
        raise RuntimeError(
            f"No bucket configured. Set {path}/s3_bucket to the bucket this "
            "environment serves."
        )

    # The environment and bucket are worth stating outright: most questions
    # about this service start with "which bucket is this container serving?".
    logger.info(
        "Loaded %d parameter(s) from %s: %s",
        len(applied),
        path,
        ", ".join(applied),
        extra={
            "platform_env": settings.platform_env,
            "bucket": settings.s3_bucket,
            "prefix": settings.s3_prefix,
        },
    )
    return applied


def reload_runtime_parameters() -> dict[str, str]:
    """Re-read only the parameters in RUNTIME_KEYS. Returns what was applied.

    Lenient where load_parameters is strict: this runs on a container that is
    already serving, so an unreadable path leaves the current settings in place
    instead of taking it down.
    """
    if not settings.platform_env:
        logger.warning("SERDSAFE_PLATFORM_ENV is unset; nothing to re-read")
        return {}

    path = settings.param_path
    applied: dict[str, str] = {}
    for name, value in sorted(_fetch(path).items()):
        if name not in RUNTIME_KEYS:
            continue
        try:
            setattr(settings, name, value)
        except ValidationError:
            logger.warning("Ignoring unusable value for %s/%s: %r", path, name, value)
            continue
        applied[name] = value
    return applied
