#!/bin/sh
# New local installation only. Existing volumes are never reset or deleted.
set -eu
cd "$(dirname "$0")/.."
image=${IRIS_IMAGE:-iris-memory:managed}
secret_volume=${IRIS_SECRETS_VOLUME:-iris-memory-secrets}
docker compose build iris
if docker volume inspect "$secret_volume" >/dev/null 2>&1; then
  printf '%s\n' '使用已有秘密卷；不改写其内容。'
else
  docker volume create "$secret_volume" >/dev/null
  docker run --rm -it --network none --user 0:0 --mount "type=volume,source=$secret_volume,target=/run/secrets" \
    "$image" python /opt/iris/deployment/provision.py
fi
docker compose up --detach --wait --wait-timeout 60 iris
printf '%s\n' '本地服务：http://127.0.0.1:8080。完成引导和首次审核前不会开放业务。'
