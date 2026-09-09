"""Serving files out of the bucket.

This service is delivery only: files arrive in the bucket by some other route,
and are read from it here. There is no upload endpoint, and the task role is
expected to hold no write permission on the bucket.

A name that is in the index is downloadable at /docs/<name>. There is no route
registered per file — a single route reads the index, which is what lets a file
appearing in the bucket become available with no restart and no route table to
rebuild.
"""

import logging
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import storage
from ..cache import cache
from ..config import settings
from ..s3_config import s3_client

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/docs/{name}", include_in_schema=False)
def download(name: str) -> StreamingResponse:
    try:
        key = storage.key_for(name)
    except storage.InvalidName:
        # Deliberately the same answer as a genuinely absent file: a probe for
        # "../../etc/passwd" learns nothing it did not already know.
        raise HTTPException(status_code=404)

    if not storage.exists(key):
        raise HTTPException(status_code=404)

    try:
        obj = s3_client.get_object(Bucket=settings.s3_bucket, Key=key)
    except ClientError as exc:
        if storage.is_gone(exc):
            # Deleted between being indexed and being asked for. Drop it so the
            # next request is answered from memory instead of calling S3.
            logger.info("Indexed key %s has gone from the bucket", key)
            cache.discard(key)
            raise HTTPException(status_code=404)
        logger.warning("GET failed for %s: %s", key, exc)
        raise HTTPException(status_code=502)
    except BotoCoreError as exc:
        logger.warning("GET failed for %s: %s", key, exc)
        raise HTTPException(status_code=502)

    return StreamingResponse(
        storage.stream(obj["Body"], key),
        media_type=obj.get("ContentType") or "application/octet-stream",
        headers={
            # quote() stops a hostile name injecting header syntax.
            "Content-Disposition": f'attachment; filename*=UTF-8\'\'{quote(name)}',
            "Content-Length": str(obj["ContentLength"]),
        },
    )
