# syntax=docker/dockerfile:1
FROM python:3.13.15-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.0@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/iris/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python

FROM python:3.13.15-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
LABEL org.opencontainers.image.source="https://github.com/AirObject/iris-core" \
      org.opencontainers.image.title="Iris" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
ENV PATH="/opt/iris/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    IRIS_CONTAINER=1 IRIS_HOST=0.0.0.0 IRIS_PORT=8080
RUN groupadd --gid 10001 iris && useradd --uid 10001 --gid iris --create-home iris \
    && mkdir /data && chown iris:iris /data && chmod 700 /data
COPY --from=build /opt/iris/venv /opt/iris/venv
USER iris:iris
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8080/login', timeout=3); raise SystemExit(0 if r.status == 200 else 1)"]
ENTRYPOINT ["iris", "--data-dir", "/data"]
CMD ["serve", "--no-open"]
