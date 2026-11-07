# harvest-sync with your config.toml and its schedule (deploy/crontab) baked in, so a change to either
# needs `docker compose up -d --build`. Your tokens are never in the image: .dockerignore keeps .env
# out, and Compose passes them in when the container starts. The image does hold config.toml, with
# your account IDs and client names, so don't push it to a public registry.

FROM python:3.13-slim AS build
ENV UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.12.1

# supercronic runs the schedule: a cron made for containers, which logs to stdout and needs no root.
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) sum=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123 ;; \
        arm64) sum=4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94 ;; \
        *) echo "no supercronic checksum for $arch" >&2; exit 1 ;; \
    esac; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
        "https://github.com/aptible/supercronic/releases/download/v0.2.51/supercronic-linux-$arch" \
        /usr/local/bin/supercronic; \
    echo "$sum  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
COPY --from=build /usr/local/bin/supercronic /usr/local/bin/supercronic
COPY --from=build /app/.venv /app/.venv
COPY config.toml deploy/crontab /config/
# A Windows checkout gives the crontab CRLF line endings, which cron would read as part of each command.
RUN sed -i 's/\r$//' /config/crontab && supercronic -test /config/crontab
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /config
USER nobody
CMD ["supercronic", "-passthrough-logs", "/config/crontab"]
