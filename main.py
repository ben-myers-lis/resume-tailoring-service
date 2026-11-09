"""Entry point for Vercel (and any ASGI server): `app` is the whole web app.

On Vercel, runs are processed by the queue subscriber in rts_worker.py, so no worker threads start
here. Elsewhere (RTS_QUEUE=threads), the app's start-up starts its worker threads as usual.
For a normal server, `python run.py` is still the simplest way to start.
"""
import logging

from service.api import create_app
from service.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
settings = get_settings()
app = create_app(settings, start_workers=settings.queue == "threads")
