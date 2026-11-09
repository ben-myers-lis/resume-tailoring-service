# Resume Tailoring Service: production image
# Build:  docker build -t resume-tailoring .
# Run:    docker run -p 8000:8000  (port: RTS_PORT, else the host's PORT, else 8000)
#         -v rts-data:/app/data --env-file .env resume-tailoring
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RTS_HOST=0.0.0.0 \
    RTS_DB=/app/data/app.db \
    RTS_FILES=/app/data/files \
    HOME=/tmp

# LibreOffice (Word -> PDF and page-count check) + Calibri-compatible fonts
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-crosextra-carlito fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app:app /app
USER app

VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; p=os.environ.get('RTS_PORT') or os.environ.get('PORT') or '8000'; urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health', timeout=4)"

CMD ["python", "run.py"]
