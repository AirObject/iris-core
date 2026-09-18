# Exact runtime base shared with the application; proxy packages match validation.
FROM python:3.12.14-slim-bookworm@sha256:d04f49f5882f49a3b91f874e75e19f0c265f7222da8659741a9d7eab148f22a9
RUN apt-get update && apt-get install -y --no-install-recommends nginx=1.22.1-9+deb12u9 nginx-common=1.22.1-9+deb12u9 gettext-base=0.21-12 && rm -rf /var/lib/apt/lists/*
USER 10001:10001
STOPSIGNAL SIGQUIT
EXPOSE 8443
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["envsubst '$IRIS_EXTERNAL_HOST' < /etc/iris/nginx.conf.template > /tmp/nginx.conf && exec /usr/sbin/nginx -c /tmp/nginx.conf -g 'daemon off;'"]
