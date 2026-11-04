# TW Retail pricing API: the code, model and data of this folder in one image.
#
#   docker build -t tw-pricing-api .
#   docker run -d --name tw-pricing-api -p 8000:8000 -v twr-state:/state -e TWR_ADMIN_PASSWORD=<min 8 chars> tw-pricing-api
# or, with docker-compose.yml and a .env file (see .env.example):
#   docker compose up -d --build
#
# Logins (users.json, .jwt_secret) live in the /state volume, so they survive rebuilds and are never baked into the image.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TWR_STATE_DIR=/state \
    PORT=8000

WORKDIR /app

# dependencies first: this layer is reused while only the code / model / data change
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# run as a non-root user that can write only to /state
RUN useradd --system --uid 10001 --no-create-home twr \
    && mkdir -p /state && chown twr /state
USER twr
VOLUME ["/state"]

EXPOSE 8000
# the model and catalogue load in ~10 s; /health answers once start-up is complete
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4)"]

# PORT can be set by the platform (Cloud Run, Render, ...); exec so uvicorn gets the stop signal and shuts down cleanly
CMD exec python -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
