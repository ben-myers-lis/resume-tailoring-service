"""Start the API and background workers:  python run.py"""
import logging

import uvicorn

from service.api import create_app
from service.config import get_settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    from service.config import public_url
    print(f"\n  Resume Tailoring is running. Open {public_url(s)} in your browser.\n")
    # One process on purpose: the job queue and SQLite live inside it. Scale with RTS_WORKERS, not more processes.
    uvicorn.run(create_app(s), host=s.host, port=s.port, proxy_headers=True,
                forwarded_allow_ips=s.forwarded_allow_ips, log_level="info")
