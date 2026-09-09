"""In-memory index of what is in the bucket, and what is known not to be.

A download request never asks S3 whether the file exists. It checks a set held
in memory, so the common case costs nothing and adds no latency. The index is
kept current three ways, cheapest first:

* a full rebuild from one paginated ListObjectsV2 — at start-up, and whenever
  the refresh endpoint is called. A single LIST returns up to 1000 keys, so
  this is far cheaper than a HEAD per request;
* write-through on upload, so a file this process stored is immediately
  serveable without waiting for anything;
* a single HEAD on a miss, which covers a file added to the bucket by other
  means since the last rebuild.

The negative half matters as much as the positive one. Without it, that HEAD
fallback would fire on every request for a name that does not exist, turning a
crawler or a broken link into unbounded S3 spend. A key S3 reports as missing
is remembered for `negative_cache_seconds`, so repeated probes are answered
from memory.

The index can also be wrong in the other direction — a file deleted from the
bucket after being indexed. Callers are expected to evict on the way past
rather than trusting the set blindly; see `discard`.
"""

import logging
import threading
import time
from typing import Iterable, Optional

from .config import settings

logger = logging.getLogger(__name__)

# Ceiling on remembered misses, so a scan of random names cannot grow the dict
# without bound. Reaching it clears the lot: this is an optimisation, and
# forgetting it early costs one HEAD rather than correctness.
MAX_TRACKED_MISSES = 4096


class FileCache:
    """Thread-safe record of the keys under the configured prefix.

    Uvicorn runs sync endpoints in a thread pool, so every method here has to
    assume concurrent callers.
    """

    def __init__(self) -> None:
        self._keys: set[str] = set()
        self._misses: dict[str, float] = {}
        self._lock = threading.Lock()
        self.last_refreshed: Optional[float] = None
        self.last_error: Optional[str] = None

    def contains(self, key: str) -> bool:
        with self._lock:
            return key in self._keys

    def add(self, key: str) -> None:
        """Record a key as present. This is the write-through path."""
        with self._lock:
            self._keys.add(key)
            # It exists now, so any earlier miss is stale.
            self._misses.pop(key, None)

    def discard(self, key: str) -> None:
        """Forget a key that turned out not to exist.

        Both halves matter: dropping it from the index stops us serving a 404
        via S3 next time, and recording the miss stops the HEAD fallback from
        immediately putting it back.
        """
        with self._lock:
            self._keys.discard(key)
            if len(self._misses) >= MAX_TRACKED_MISSES:
                self._misses.clear()
            self._misses[key] = time.monotonic()

    def recently_missing(self, key: str) -> bool:
        """True if S3 said this key was absent recently enough to trust."""
        with self._lock:
            seen = self._misses.get(key)
            if seen is None:
                return False
            if time.monotonic() - seen > settings.negative_cache_seconds:
                del self._misses[key]
                return False
            return True

    def replace(self, keys: Iterable[str]) -> int:
        """Swap in a freshly listed set of keys. Returns the new size."""
        with self._lock:
            self._keys = set(keys)
            # Anything that exists now is no longer a miss, and anything that
            # does not will be re-learned on demand.
            self._misses.clear()
            self.last_refreshed = time.time()
            return len(self._keys)

    def size(self) -> int:
        with self._lock:
            return len(self._keys)


cache = FileCache()
