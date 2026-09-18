FROM node:22.14.0-bookworm-slim@sha256:663c09e4fd483fbcb2bb7297b3618061ac23f0a1925b0958db2ab734efad7c94 AS frontend
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY web/tsconfig.json web/build.mjs ./
COPY web/src ./src
COPY web/public ./public
COPY protocol /protocol
COPY clients /clients
RUN npm run build

FROM python:3.12.14-slim-bookworm@sha256:d04f49f5882f49a3b91f874e75e19f0c265f7222da8659741a9d7eab148f22a9 AS sqlite
RUN apt-get update && apt-get install -y --no-install-recommends gcc make libc6-dev curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl --fail --location --output sqlite.tar.gz https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz && \
    echo '0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c  sqlite.tar.gz' | sha256sum -c - && \
    tar -xzf sqlite.tar.gz && cd sqlite-autoconf-3530400 && \
    ./configure --prefix=/opt/iris-sqlite --disable-static --disable-readline && make -j2 && make install

FROM python:3.12.14-slim-bookworm@sha256:d04f49f5882f49a3b91f874e75e19f0c265f7222da8659741a9d7eab148f22a9 AS application
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LD_LIBRARY_PATH=/opt/iris-sqlite/lib
COPY --from=sqlite /opt/iris-sqlite /opt/iris-sqlite
COPY deployment/requirements.lock /opt/iris/requirements.lock
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /opt/iris/requirements.lock && \
    groupadd --gid 10001 iris && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin iris && \
    mkdir -p /data /run/secrets && chown iris:iris /data /run/secrets && chmod 700 /data /run/secrets
WORKDIR /opt/iris
COPY companion_memory ./companion_memory
COPY protocol ./protocol
COPY clients ./clients
COPY deployment/provision.py ./deployment/provision.py
COPY --from=frontend /build/dist ./web/dist
RUN python -m companion_memory.runtime.managed_cli release > /opt/iris/release.json
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 CMD ["python", "-m", "companion_memory.runtime.managed_cli", "health"]
CMD ["python", "-m", "companion_memory.runtime.managed_main"]
