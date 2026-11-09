"""Vercel Queues subscriber: processes one tailoring run per message.

Declared in pyproject.toml under [[tool.vercel.subscribers]]; Vercel builds it into a private,
queue-triggered function (it is not reachable from the internet). The web app sends
{"run_id": ...} to the "rts-runs" topic when a run is created; see service/jobs.py.
"""
from __future__ import annotations

import asyncio
import os

from vercel.queue import Message, subscribe

from service.jobs import TOPIC, process_message


@subscribe(topic=TOPIC, consumer_group="tailor", retry_after=60,
           max_concurrency=int(os.environ.get("RTS_WORKERS", "4")))   # resumes processed at once
async def tailor_run(message: Message[dict[str, object]]) -> None:
    # The pipeline is blocking (Claude calls, rendering): run it off the event loop.
    await asyncio.to_thread(process_message, message.payload)
