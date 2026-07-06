# Stage 1 — build the React SPA
FROM node:22-alpine AS frontend-build
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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
