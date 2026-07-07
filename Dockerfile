# Stage 1 — build the React SPA. Node 24: package-lock.json is written by
# npm 11, which older bundled npms (node 22's 10.x) refuse as out-of-sync.
FROM node:24-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2 — build Python wheels (quickjs compiles from source if no wheel
# matches, so keep the toolchain out of the final image)
FROM python:3.12-slim AS python-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc make \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip wheel --no-cache-dir -r /tmp/requirements.txt -w /wheels

# Stage 3 — runtime
FROM python:3.12-slim
WORKDIR /app

COPY --from=python-build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# Layout mirrors the repo: main.py finds the SPA at ../../frontend/dist
# relative to backend/app/main.py.
COPY backend/app /app/backend/app
COPY --from=frontend-build /build/dist /app/frontend/dist

# Database lives on a volume; parent dir is created by the app if missing.
ENV AIDND_DB_PATH=/data/data.db
VOLUME /data

EXPOSE 8000
WORKDIR /app/backend
# --proxy-headers: behind a reverse proxy (any hosted deploy), trust
# X-Forwarded-For so per-IP rate limits key on the client, not the proxy.
# Single worker on purpose: the turn lock, rate limiter, and debug log are
# in-process state.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
