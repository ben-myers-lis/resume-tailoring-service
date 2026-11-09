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
    uvicorn.run(create_app(s), host=s.host, port=s.port)
