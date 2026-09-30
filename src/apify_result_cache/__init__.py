"""Result cache for Apify Actors.

    from apify_result_cache import ResultCache, youtube_key_fields

    async with Actor:
        cache = await ResultCache.start(namespace="youtube-transcript", schema_version=1)
        try:
            for url in urls:
                key = cache.key(youtube_key_fields(video_id=vid, languages=langs, ...))
                cache.log(key, entity_id=vid, outcome="logged")
                ...
        finally:
            await cache.close()

Absent `RESULT_CACHE_MODE` the cache is inert: one log line, no behaviour, no
network. In `keys` mode it records request keys so the hit rate can be
measured. Serving from cache arrives in 0.2.
"""

from ._version import __version__
from .cache import CacheEntry, ResultCache
from .codec import CodecError
from .db import CacheDBError
from .youtube import youtube_key_fields

__all__ = [
    "CacheDBError",
    "CacheEntry",
    "CodecError",
    "ResultCache",
    "__version__",
    "youtube_key_fields",
]
