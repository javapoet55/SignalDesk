"""Atomic cache writes with stale-snapshot protection for cooperating writers."""
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path


class CacheConflictError(RuntimeError):
    pass


class LoadedCache(dict):
    pass


def load_cache_file(path):
    path = Path(path)
    raw = path.read_bytes()
    cache = LoadedCache(json.loads(raw))
    cache._cache_path = path.resolve()
    cache._cache_digest = hashlib.sha256(raw).digest()
    return cache


def atomic_save_cache(path, cache):
    path = Path(path)
    temporary = None
    # The lock file is stable across replacement of the JSON inode.
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if isinstance(cache, LoadedCache):
            if cache._cache_path != path.resolve():
                raise ValueError('Cache must be saved to the path it was loaded from')
            if hashlib.sha256(path.read_bytes()).digest() != cache._cache_digest:
                raise CacheConflictError('Cache changed during refresh; refusing stale snapshot. Retry with a fresh load.')
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                    dir=path.parent, prefix=path.name + '.', suffix='.tmp',
                    delete=False) as stream:
                temporary = stream.name
                json.dump(cache, stream)
                stream.flush()
                os.fsync(stream.fileno())
            digest = hashlib.sha256(Path(temporary).read_bytes()).digest()
            os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o600)
            os.replace(temporary, path)
            temporary = None
            if isinstance(cache, LoadedCache):
                cache._cache_digest = digest
        finally:
            if temporary is not None:
                os.unlink(temporary)
