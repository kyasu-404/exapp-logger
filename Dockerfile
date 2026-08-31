FROM node:22-alpine AS frontend
WORKDIR /build/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.12-alpine3.21
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow
WORKDIR /app

RUN apk add --no-cache bash curl frp libpq tzdata \
    && apk add --no-cache --virtual .build-deps build-base postgresql-dev

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && apk del .build-deps

COPY --chmod=755 start.sh /start.sh
COPY img ./img
COPY --from=frontend /build/frontend/dist/app.js ./js/app.js
COPY --from=frontend /build/frontend/dist/app.css ./css/app.css

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD sh -c 'curl -sf --unix-socket /tmp/exapp.sock http://localhost/heartbeat || curl -sf "http://127.0.0.1:${APP_PORT}/heartbeat"'

ENTRYPOINT ["/start.sh"]
CMD ["file-control"]
