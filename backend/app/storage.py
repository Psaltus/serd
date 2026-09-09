"""S3 access, and the rules for turning a requested name into a key.

Everything that talks to the bucket lives here so the routes stay about HTTP
and the cache stays about bookkeeping.
"""

import logging
import os
from typing import Iterator, Optional

from botocore.exceptions import BotoCoreError, ClientError

from .s3_config import s3_client
from .cache import cache
from .config import settings

logger = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
# S3 error codes that all mean "that key is not there".
GONE_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class InvalidName(ValueError):
    """The requested name could not be turned into a key under the prefix."""


def key_for(name: Optional[str]) -> str:
    """Map a client-supplied file name to a key inside the configured prefix.

    Path-shaped names are rejected rather than flattened. Silently storing
    "../../etc/passwd" as "passwd" would be safe, but it would hand the caller
    back a file under a name they never asked for, which is its own surprise.
    """
    candidate = (name or "").strip()
    if (
        not candidate
        or "/" in candidate
        or "\\" in candidate
        or candidate.startswith(".")
        or candidate != os.path.basename(candidate)
    ):
        raise InvalidName(name or "")
    return f"{settings.s3_prefix}{candidate}"


def is_gone(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in GONE_CODES


def list_keys() -> list[str]:
    """Every key under the prefix, from one paginated LIST.

    Raises on failure; callers decide whether that is fatal.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    return [
        obj["Key"]
        for page in paginator.paginate(
            Bucket=settings.s3_bucket, Prefix=settings.s3_prefix
        )
        for obj in page.get("Contents", [])
        # Directory placeholder objects are not downloadable files.
        if not obj["Key"].endswith("/")
    ]


def rebuild_cache() -> int:
    """Rebuild the index from the bucket. Returns the number of keys indexed.

    Never raises: an unreachable bucket leaves the previous index in place
    rather than emptying it, so a blip cannot take working downloads offline.
    """
    if not settings.s3_bucket:
        logger.warning("No bucket configured; index left empty")
        return 0

    try:
        keys = list_keys()
    except (ClientError, BotoCoreError) as exc:
        # Covers missing credentials and connectivity as well as S3 errors.
        cache.last_error = str(exc)
        logger.warning(
            "Could not list s3://%s/%s: %s",
            settings.s3_bucket,
            settings.s3_prefix,
            exc,
        )
        return cache.size()

    cache.last_error = None
    count = cache.replace(keys)
    logger.info(
        "Index rebuilt: %d file(s)",
        count,
        extra={"files": count, "bucket": settings.s3_bucket},
    )
    logger.debug("Indexed keys: %s", sorted(keys))
    return count


def exists(key: str) -> bool:
    """Whether the key is available, consulting S3 only when the index misses.

    This is what lets a file added to the bucket by other means be served
    before anyone calls the refresh endpoint.
    """
    if cache.contains(key):
        logger.debug("Cache hit for %s", key, extra={"key": key, "cache": "hit"})
        return True
    if cache.recently_missing(key):
        logger.debug(
            "Negative cache hit for %s; not asking S3",
            key,
            extra={"key": key, "cache": "negative"},
        )
        return False

    logger.debug(
        "Cache miss for %s; checking S3", key, extra={"key": key, "cache": "miss"}
    )
    try:
        s3_client.head_object(Bucket=settings.s3_bucket, Key=key)
    except ClientError as exc:
        if not is_gone(exc):
            logger.warning("HEAD failed for %s: %s", key, exc, extra={"key": key})
        else:
            logger.debug("S3 has no %s", key, extra={"key": key})
        cache.discard(key)
        return False
    except BotoCoreError as exc:
        logger.warning("HEAD failed for %s: %s", key, exc, extra={"key": key})
        return False

    # Found out-of-band. Write it through so the next request is free.
    logger.info("Indexed %s on demand", key, extra={"key": key})
    cache.add(key)
    return True


def stream(body, key: str) -> Iterator[bytes]:
    """Yield an object's bytes, absorbing a failure part-way through.

    Response headers are already on the wire by the time this runs, so a
    mid-transfer failure cannot become an error status — the client sees a
    truncated download either way. What matters is that the exception does not
    escape into the server: it is logged, the index is corrected if the object
    vanished, and the generator ends.
    """
    try:
        for chunk in body.iter_chunks(chunk_size=CHUNK_SIZE):
            yield chunk
    except ClientError as exc:
        if is_gone(exc):
            cache.discard(key)
        logger.warning("Download of %s failed mid-stream: %s", key, exc)
    except (BotoCoreError, OSError) as exc:
        # Connection reset or timeout. The object may well still be there, so
        # the index is left alone.
        logger.warning("Download of %s interrupted: %s", key, exc)
    finally:
        try:
            body.close()
        except Exception:  # pragma: no cover - closing must never raise
            logger.debug("Closing body for %s failed", key, exc_info=True)
