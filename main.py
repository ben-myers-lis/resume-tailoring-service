"""Entry point for Vercel (and any ASGI server): `app` is the whole web app.

The real app (database connection, schema check) is built on the first request, not at import:
Vercel imports this module while building, when no database may be reachable yet. If the app
can't start (for example no database is connected), each request gets a short explanation instead
of a crash, and the next request tries again.

On Vercel, runs are processed by the queue subscriber in rts_worker.py, so no worker threads start
here. Elsewhere (RTS_QUEUE=threads) the app's start-up starts its worker threads as usual.
For a normal server, `python run.py` is still the simplest way to start.
"""
import asyncio
import logging
import threading

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rts.main")

_real = None
_lock = threading.Lock()          # a thread lock: requests may arrive on different event loops


def _build_once():
    global _real
    with _lock:
        if _real is None:
            from service.api import create_app
            from service.config import get_settings
            settings = get_settings()
            _real = create_app(settings, start_workers=settings.queue == "threads")
    return _real


async def _get_app():
    return _real if _real is not None else await asyncio.to_thread(_build_once)


async def app(scope, receive, send):
    try:
        real = await _get_app()
    except Exception as e:                      # e.g. no database connected yet
        log.exception("the app could not start")
        if scope["type"] == "lifespan":
            raise
        if scope["type"] == "http":
            body = f"Resume Tailoring could not start: {e}\n".encode()
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": body})
        return
    await real(scope, receive, send)
