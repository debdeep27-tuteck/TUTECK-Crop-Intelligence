"""
shared/cache.py

Small in-process TTL cache shared across the Flask microservices. Not a
replacement for Redis — it's per-process, in-memory, and intentionally
simple. That's fine here because every service (auth, cold-storage,
auction, etc.) currently runs as a single Flask process under main.py,
not multiple gunicorn workers.

Two entry points:

- @ttl_cache(seconds=...)   caches a plain function's return value, keyed
  by its arguments. Good for lookups like get_user(uid) or
  get_role_permissions().

- @cached_route(seconds=...) caches a Flask view's response, keyed by
  path + query string. Good for read-heavy listing endpoints that return
  the same thing to every caller. Do NOT use this on routes whose
  response depends on the logged-in user (reads g.user, filters by
  farmerEmail, etc.) since the default cache key doesn't include
  identity — two different users hitting the same URL would wrongly
  share a cached response.

Both support invalidate_prefix(prefix) to drop every cached entry whose
key starts with that prefix — call this after any write that would make
a cached read stale.
"""

import time
import threading
from functools import wraps

_lock = threading.Lock()
_store = {}  # key -> (expires_at, value)


def _now():
    return time.time()


def _get(key):
    with _lock:
        entry = _store.get(key)
        if entry is None:
            return None, False
        expires_at, value = entry
        if expires_at < _now():
            del _store[key]
            return None, False
        return value, True


def _set(key, value, seconds):
    with _lock:
        _store[key] = (_now() + seconds, value)


def invalidate_prefix(prefix):
    """Drop every cached entry whose key starts with `prefix`."""
    with _lock:
        dead = [k for k in _store if k.startswith(prefix)]
        for k in dead:
            del _store[k]
    return len(dead)


def invalidate_key(key):
    with _lock:
        _store.pop(key, None)


def _make_key(prefix, args, kwargs):
    return f"{prefix}:{args!r}:{sorted(kwargs.items())!r}"


def ttl_cache(seconds=30, key_prefix=None):
    """Cache a function's return value for `seconds`, keyed by its args.

    key_prefix lets you set an explicit, stable prefix for
    invalidate_prefix() calls elsewhere, instead of relying on the
    auto-generated module.qualname (which is fine too, just less
    readable at call sites).
    """
    def decorator(fn):
        prefix = key_prefix or f"{fn.__module__}.{fn.__qualname__}"

        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = _make_key(prefix, args, kwargs)
            value, hit = _get(key)
            if hit:
                return value
            result = fn(*args, **kwargs)
            _set(key, result, seconds)
            return result

        wrapper.cache_prefix = prefix
        wrapper.invalidate_all = lambda: invalidate_prefix(prefix)
        return wrapper
    return decorator


def cached_route(seconds=30, key_prefix=None):
    """Cache a Flask view function's response for `seconds`, keyed by
    request path + query string. Only 2xx responses are cached — error
    responses (404, 400, 500, ...) are never cached.

    Do not use on routes whose output depends on the logged-in user.
    """
    from flask import request, make_response

    def decorator(fn):
        prefix = key_prefix or f"route:{fn.__module__}.{fn.__qualname__}"

        @wraps(fn)
        def wrapper(*args, **kwargs):
            cache_key = f"{prefix}:{request.path}?{request.query_string.decode()}"
            value, hit = _get(cache_key)
            if hit:
                return value
            result = fn(*args, **kwargs)
            response = make_response(result)
            if 200 <= response.status_code < 300:
                _set(cache_key, response, seconds)
            return response

        wrapper.cache_prefix = prefix
        wrapper.invalidate_all = lambda: invalidate_prefix(prefix)
        return wrapper
    return decorator